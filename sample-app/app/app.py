import json
import logging
import random
import sys
import time

from flask import Flask, Response, request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE_NAME = "sample-app"

trace.set_tracer_provider(
    TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
)
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(endpoint="otel-collector.observability.svc.cluster.local:4317", insecure=True)
    )
)
tracer = trace.get_tracer(SERVICE_NAME)

REQUEST_COUNT = Counter(
    "app_requests_total", "Total HTTP requests", ["route", "method", "status"]
)
REQUEST_LATENCY = Histogram(
    "app_request_duration_seconds", "Request duration in seconds", ["route"]
)

# A trace_id only exists once a request is being handled, so this formatter
# reads it off the active span at log time rather than at logger construction.
class TraceContextFormatter(logging.Formatter):
    def format(self, record):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else None
        record.span_id = format(ctx.span_id, "016x") if ctx.span_id else None
        return json.dumps(
            {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
                "level": record.levelname,
                "msg": record.getMessage(),
                "trace_id": record.trace_id,
                "span_id": record.span_id,
                **getattr(record, "extra_fields", {}),
            }
        )

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(TraceContextFormatter())
logger = logging.getLogger(SERVICE_NAME)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

ROUTE_PROFILES = {
    "checkout": {"base_ms": 80, "jitter_ms": 120, "error_rate": 0.08},
    "login": {"base_ms": 20, "jitter_ms": 40, "error_rate": 0.03},
    "cart": {"base_ms": 15, "jitter_ms": 30, "error_rate": 0.02},
    "inventory": {"base_ms": 40, "jitter_ms": 200, "error_rate": 0.12},
}


def handle_route(name):
    profile = ROUTE_PROFILES[name]
    start = time.time()
    with tracer.start_as_current_span(f"handle_{name}") as span:
        with tracer.start_as_current_span(f"{name}.db_lookup"):
            time.sleep((profile["base_ms"] + random.random() * profile["jitter_ms"]) / 1000)

        failed = random.random() < profile["error_rate"]
        status = 500 if failed else 200
        span.set_attribute("http.route", f"/{name}")
        span.set_attribute("app.failed", failed)

        duration = time.time() - start
        REQUEST_LATENCY.labels(route=name).observe(duration)
        REQUEST_COUNT.labels(route=name, method="GET", status=status).inc()

        level = logging.ERROR if failed else logging.INFO
        logger.log(
            level,
            "handled request",
            extra={"extra_fields": {"route": f"/{name}", "status": status, "duration_ms": round(duration * 1000, 2)}},
        )
        return {"route": name, "status": status}, status


@app.route("/checkout")
def checkout():
    return handle_route("checkout")


@app.route("/login")
def login():
    return handle_route("login")


@app.route("/cart")
def cart():
    return handle_route("cart")


@app.route("/inventory")
def inventory():
    return handle_route("inventory")


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
