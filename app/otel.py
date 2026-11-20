"""Run traces in the industry-standard format: OpenTelemetry, following the GenAI
semantic conventions, so they open in Langfuse, Arize Phoenix, Jaeger or any
OTLP backend next to the app's own trace page.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. http://localhost:6006 for a
local Phoenix, or Langfuse's OTLP endpoint with OTEL_EXPORTER_OTLP_HEADERS).
Each finished `Run` (app/tracing.py) becomes one `invoke_agent` span with a
child span per step: `chat <model>` for model calls (tokens, searches, cost) and
`execute_tool <name>` for tool calls. Steps are replayed from the recorded
timings after the run, so exporting can't slow down or break the run itself.
The GenAI conventions are still marked "Development" upstream; attribute names
follow the current registry.
"""
import logging
import os
import time
from datetime import UTC

log = logging.getLogger(__name__)

_tracer = None
_disabled = False


def _get_tracer():
    global _tracer, _disabled
    if _tracer is not None or _disabled:
        return _tracer
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        _disabled = True
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get(
            "OTEL_SERVICE_NAME", "sidequest")}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads the OTEL_* env vars
        _tracer = provider.get_tracer("sidequest")
    except Exception:  # noqa: BLE001 - tracing export is optional
        log.exception("OpenTelemetry export couldn't start; continuing without it")
        _disabled = True
    return _tracer


def export_run(trace, started_at, status: str, error: str | None, tracer=None) -> None:
    """One finished run as spans. `trace` is tracing.Trace; `started_at` naive UTC."""
    tracer = tracer or _get_tracer()
    if tracer is None:
        return
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import Status, StatusCode

        start_ns = int(started_at.replace(tzinfo=UTC).timestamp() * 1e9)  # stored as naive UTC
        end_ns = start_ns + int((time.monotonic() - trace.started) * 1e9)
        root = tracer.start_span(f"invoke_agent {trace.kind}", start_time=start_ns, attributes={
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": f"sidequest.{trace.kind}",
            "gen_ai.provider.name": trace.provider or "", "gen_ai.request.model": trace.model or "",
            "gen_ai.usage.input_tokens": trace.input_tokens, "gen_ai.usage.output_tokens": trace.output_tokens,
            "sidequest.cost_usd": round(trace.cost_usd, 6), "sidequest.searches": trace.searches,
            "sidequest.action_id": trace.action_id or 0, "sidequest.video_id": trace.video_id or 0,
        })
        ctx = otel_trace.set_span_in_context(root)
        for step in trace.steps:
            ms = int(step.get("ms") or 0)
            s_end = start_ns + int(step.get("at_ms", 0)) * 1_000_000
            s_start = max(start_ns, s_end - ms * 1_000_000)
            if step.get("t") == "model":
                span = tracer.start_span(f"chat {step.get('model', '')}", context=ctx, start_time=s_start, attributes={
                    "gen_ai.operation.name": "chat", "gen_ai.request.model": step.get("model", ""),
                    "gen_ai.provider.name": trace.provider or "",
                    "gen_ai.usage.input_tokens": int(step.get("in") or 0),
                    "gen_ai.usage.output_tokens": int(step.get("out") or 0),
                    "sidequest.cost_usd": float(step.get("cost") or 0), "sidequest.searches": int(step.get("searches") or 0),
                })
            else:
                span = tracer.start_span(f"execute_tool {step.get('name', '')}", context=ctx, start_time=s_start,
                                         attributes={"gen_ai.operation.name": "execute_tool",
                                                     "gen_ai.tool.name": step.get("name", ""),
                                                     "gen_ai.tool.call.arguments": step.get("args", "")})
                if not step.get("ok", True):
                    span.set_status(Status(StatusCode.ERROR, step.get("note", "")))
            span.end(end_time=s_end)
        if status != "ok":
            root.set_status(Status(StatusCode.ERROR, (error or "")[:200]))
            root.set_attribute("error.type", (error or "error").split(":")[0][:60])
        root.end(end_time=end_ns)
    except Exception:  # noqa: BLE001
        log.exception("Couldn't export the %s run to OpenTelemetry", trace.kind)

