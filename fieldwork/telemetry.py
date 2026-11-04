"""Observability: live traffic metrics (in-process, per minute, last 24 h), a Prometheus endpoint, and OpenTelemetry
traces of every agent run.

Traces are exported when an OTLP endpoint is configured (any OTel backend: Langfuse, Grafana Tempo, Honeycomb...):

    OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 public:secret>

FIELDWORK_OTEL=console prints spans instead. LLM spans carry the OTel GenAI attributes (gen_ai.*), so Langfuse and
similar tools show model, tokens and cost per call.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

WINDOW_MINUTES = 24 * 60
_LATENCY_SAMPLES = 200   # per minute bucket, enough for p50/p95 without unbounded memory


class _Bucket:
    __slots__ = ("minute", "requests", "errors", "latency_ms", "agent_runs", "rows", "review_rows",
                 "llm_live", "llm_recorded", "cost_usd", "tool_calls", "endpoints")

    def __init__(self, minute: int):
        self.minute = minute
        self.requests = self.errors = self.agent_runs = self.rows = self.review_rows = 0
        self.llm_live = self.llm_recorded = self.tool_calls = 0
        self.cost_usd = 0.0
        self.latency_ms: list[float] = []
        self.endpoints: dict[str, int] = {}


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))], 1)


class Monitor:
    """Thread-safe ring buffer of per-minute buckets. Resets on restart; export traces for durable history."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: deque[_Bucket] = deque(maxlen=WINDOW_MINUTES)
        self.started = time.time()
        self.totals = {"requests": 0, "errors": 0, "agent_runs": 0, "rows": 0, "review_rows": 0, "llm_live": 0,
                       "llm_recorded": 0, "cost_usd": 0.0, "tool_calls": 0}

    def _bucket(self) -> _Bucket:
        minute = int(time.time() // 60)
        if not self._buckets or self._buckets[-1].minute != minute:
            self._buckets.append(_Bucket(minute))
        return self._buckets[-1]

    def record_request(self, route: str, status: int, ms: float) -> None:
        with self._lock:
            b = self._bucket()
            b.requests += 1
            b.endpoints[route] = b.endpoints.get(route, 0) + 1
            if status >= 500:
                b.errors += 1
                self.totals["errors"] += 1
            if len(b.latency_ms) < _LATENCY_SAMPLES:
                b.latency_ms.append(ms)
            self.totals["requests"] += 1

    def record_agent(self, rows: int, review_rows: int, llm_live: int, llm_recorded: int, cost_usd: float,
                     tool_calls: int) -> None:
        with self._lock:
            b = self._bucket()
            for k, v in (("agent_runs", 1), ("rows", rows), ("review_rows", review_rows), ("llm_live", llm_live),
                         ("llm_recorded", llm_recorded), ("tool_calls", tool_calls)):
                setattr(b, k, getattr(b, k) + v)
                self.totals[k] += v
            b.cost_usd += cost_usd
            self.totals["cost_usd"] += cost_usd

    def snapshot(self, minutes: int = 60) -> dict:
        minutes = max(5, min(WINDOW_MINUTES, minutes))
        now = int(time.time() // 60)
        with self._lock:
            by_minute = {b.minute: b for b in self._buckets if b.minute > now - minutes}
            series = []
            for m in range(now - minutes + 1, now + 1):
                b = by_minute.get(m)
                series.append({"t": m * 60, "requests": b.requests if b else 0, "errors": b.errors if b else 0,
                               "p95_ms": _pct(b.latency_ms, .95) if b else None,
                               "rows": b.rows if b else 0, "review_rows": b.review_rows if b else 0,
                               "llm_live": b.llm_live if b else 0, "llm_recorded": b.llm_recorded if b else 0,
                               "cost_usd": round(b.cost_usd, 6) if b else 0.0})
            lat = [x for b in by_minute.values() for x in b.latency_ms]
            endpoints: dict[str, int] = {}
            for b in by_minute.values():
                for k, v in b.endpoints.items():
                    endpoints[k] = endpoints.get(k, 0) + v
            window = {k: sum(s[k] for s in series) for k in ("requests", "errors", "rows", "review_rows", "llm_live",
                                                              "llm_recorded")}
            window["cost_usd"] = round(sum(s["cost_usd"] for s in series), 6)
            window["p50_ms"], window["p95_ms"] = _pct(lat, .5), _pct(lat, .95)
            window["review_rate"] = round(window["review_rows"] / window["rows"], 4) if window["rows"] else None
            window["error_rate"] = round(window["errors"] / window["requests"], 4) if window["requests"] else None
            return {"minutes": minutes, "uptime_s": int(time.time() - self.started), "window": window,
                    "totals": {**self.totals, "cost_usd": round(self.totals["cost_usd"], 6)},
                    "endpoints": dict(sorted(endpoints.items(), key=lambda kv: -kv[1])[:12]), "series": series,
                    "tracing": tracing_target()}

    def prometheus(self) -> str:
        t = self.snapshot(5)["totals"]
        lines = []
        for name, key, help_ in (("requests_total", "requests", "HTTP requests"), ("errors_total", "errors", "5xx responses"),
                                 ("agent_runs_total", "agent_runs", "agent runs"), ("rows_total", "rows", "CRM rows processed"),
                                 ("review_rows_total", "review_rows", "rows sent to human review"),
                                 ("llm_live_calls_total", "llm_live", "paid LLM calls"),
                                 ("llm_recorded_calls_total", "llm_recorded", "LLM answers served from the recording"),
                                 ("tool_calls_total", "tool_calls", "agent tool calls"), ("llm_cost_usd_total", "cost_usd", "LLM spend")):
            lines += [f"# HELP fieldwork_{name} {help_}", f"# TYPE fieldwork_{name} counter", f"fieldwork_{name} {t[key]}"]
        return "\n".join(lines) + "\n"


monitor = Monitor()

# ---------- OpenTelemetry ----------
_tracer = None
_target = "off"


def tracing_target() -> str:
    return _target


def init_tracing(service: str = "fieldwork") -> None:
    """Configure an OTel tracer if an exporter is requested; otherwise tracing stays a no-op."""
    global _tracer, _target
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    mode = os.environ.get("FIELDWORK_OTEL", "")
    if not endpoint and mode != "console":
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:   # optional dependency: pip install ".[otel]"
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _target = "otlp"
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        _target = "console"
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("fieldwork")


def export_run(name: str, trace_obj, attributes: dict, started_ns: int, ended_ns: int) -> None:
    """Emit one root span for the run and a child span per tool call, LLM call and decision, using the timings the
    agent's trace already recorded (exported after the run, so tracing never slows the agent down)."""
    if _tracer is None:
        return
    from opentelemetry import trace
    root = _tracer.start_span(name, start_time=started_ns, attributes={k: v for k, v in attributes.items() if v is not None})
    ctx = trace.set_span_in_context(root)
    for e in trace_obj.events:
        end = e.get("t_ns") or ended_ns
        start = end - int((e.get("ms") or 0) * 1e6)
        attrs = {"fieldwork.kind": e["kind"], "fieldwork.account": str(e.get("account"))}
        if e["kind"] == "llm":
            attrs.update({"gen_ai.system": "groq", "gen_ai.request.model": e.get("model", ""),
                          "gen_ai.usage.input_tokens": e.get("tokens_in", 0),
                          "gen_ai.usage.output_tokens": e.get("tokens_out", 0),
                          "fieldwork.cost_usd": e.get("cost_usd", 0.0), "fieldwork.replayed": bool(e.get("replayed"))})
        span = _tracer.start_span(f"{e['kind']}.{e['name']}", context=ctx, start_time=start, attributes=attrs)
        span.end(end_time=end)
    root.end(end_time=ended_ns)
