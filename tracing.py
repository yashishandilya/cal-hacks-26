"""
Arize Phoenix tracing (observability track).

Launches a local Phoenix server and auto-instruments our LLM calls (Gemini via the
google-genai SDK and instructor), so every agent step shows up as a trace at
http://localhost:6006. Call startTracing() once at app startup.
"""

import sys
import phoenix as px
from phoenix.otel import register

# Phoenix prints an emoji banner on launch; the Windows console (cp1252) can't encode it
# and crashes. Force stdout/stderr to UTF-8 so the launch never dies on the banner.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_started = False


# Launches the local Phoenix UI and wires OpenTelemetry auto-instrumentation so all
# Gemini/instructor calls are captured as spans. Idempotent: safe to call more than once.
def startTracing():
    global _started
    if _started:
        return
    px.launch_app()
    # auto_instrument is off: the openinference google-genai instrumentor is incompatible
    # with our google-genai version. We emit our own spans (see runtime.py) instead, which
    # also gives a cleaner tick -> ingest/safety/council trace tree.
    register(project_name="the-experimenter", auto_instrument=False)
    _started = True
    print("[tracing] Phoenix live at http://localhost:6006")


# Fetches recent traces for one experiment from Phoenix: each runTick root span carries
# the expId, and its child spans (ingest/safety/council) share the trace_id. Returns the
# most recent traces with their spans + per-span latency for the in-app trace panel.
def getExperimentTraces(expId: str, limit: int = 10) -> list:
    import pandas as pd
    from phoenix.client import Client

    df = Client(base_url="http://localhost:6006").spans.get_spans_dataframe(
        project_identifier="the-experimenter"
    )
    if df is None or len(df) == 0 or "attributes.expId" not in df.columns:
        return []

    roots = df[df["attributes.expId"] == expId].sort_values("start_time", ascending=False).head(limit)

    def cell(row, col):
        val = row.get(col)
        return None if val is None or pd.isna(val) else val

    out = []
    for _, root in roots.iterrows():
        traceId = root["context.trace_id"]
        spansDf = df[df["context.trace_id"] == traceId].sort_values("start_time")
        spans = []
        for _, s in spansDf.iterrows():
            latency = None
            if not pd.isna(s["start_time"]) and not pd.isna(s["end_time"]):
                latency = round((s["end_time"] - s["start_time"]).total_seconds() * 1000, 1)
            spans.append({
                "name": s["name"],
                "latencyMs": latency,
                "verdict": cell(s, "attributes.verdict"),
                "councilVerdict": cell(s, "attributes.councilVerdict"),
                "detail": cell(s, "attributes.detail"),
            })
        out.append({
            "traceId": traceId,
            "startTime": str(root["start_time"]),
            "verdict": cell(root, "attributes.verdict"),
            "spans": spans,
        })
    return out
