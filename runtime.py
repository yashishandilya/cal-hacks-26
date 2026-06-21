"""
Master Orchestrator runtime.

For now this holds the ingest step (chat transcript -> TelemetryPacket). Later
blocks add the full loop: ingest -> Council -> deterministic validation ->
Redis write -> pipeline_trace envelope.
"""

import os
import time
import instructor
from typing import List, Optional
from pydantic import BaseModel
from dotenv import load_dotenv

# Reuse the schemas already defined rather than re-declaring them.
from datetime import datetime, timezone
from models import (
    ProtocolSchema, TelemetryPacket, ProtocolViolationException,
    PipelineTrace, TraceStage,
)
from main import dailyLogEntry, experimentState

# The Master orchestrator reads protocols from Redis and runs the deterministic engine.
import store
from validation import ValidationEngine
# Council members: De-escalator (council.py) and the reused arbiter (compaction.py).
from council import deEscalate
from compaction import askVerdict

# OpenTelemetry tracer for Phoenix. If tracing was never started, get_tracer returns a
# no-op tracer, so these spans are harmless in tests / when Phoenix is off.
from opentelemetry import trace
_tracer = trace.get_tracer("experimenter.runtime")


# One extracted measurement as a flat key/value pair. value is a string so a single
# schema covers both numeric metrics ("9") and categorical ones ("mild"); the
# ValidationEngine already coerces with float()/str() per operator, so nothing breaks.
class MetricReading(BaseModel):
    key: str
    value: str


# The model-facing extraction schema. We ask the LLM for a LIST of readings instead
# of an open dict, because Gemini's structured output returns {} for open {str: Any}
# objects but fills a well-defined list correctly (verified empirically).
class TelemetryExtraction(BaseModel):
    readings: List[MetricReading]
    actions: List[str]
    notes: Optional[str] = None


def humanizeKey(key: str) -> str:
    return key.removeprefix("v_").replace("_", " ")


# Pulls the set of variable IDs the protocol actually knows about out of the
# incompatibilities map (both the keys and every ID listed inside their lists).
def knownVariableIds(protocol: ProtocolSchema) -> List[str]:
    ids = set(protocol.incompatibilities.keys())
    for clashList in protocol.incompatibilities.values():
        ids.update(clashList)
    return sorted(ids)


# Converts a free-text daily-log transcript into a TelemetryPacket whose metric
# keys are constrained to the protocol's own metricKeys, dropping any key the LLM
# invents (the drop is done in code, so we never trust the model to stay in bounds).
def collectTelemetry(chatTranscript: str, protocol: ProtocolSchema) -> TelemetryPacket:
    startTime = time.perf_counter()
    load_dotenv()
    apiKey = os.getenv("GEMINI_API_KEY")
    if not apiKey:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it to your .env file before running ingest.")

    # The exact vocabulary the model is allowed to use: metric keys from the
    # thresholds, variable IDs from the incompatibilities map.
    metricKeys = [t.metricKey for t in protocol.thresholds]
    variableIds = knownVariableIds(protocol)

    # Each metric key is an opaque ID, so we hint its meaning primarily from the key
    # name itself (humanized) and only secondarily from the errorMessage, which often
    # describes the alert/clash rather than the measurement and can mislead on its own.
    metricHints = [f"'{t.metricKey}' = measures \"{humanizeKey(t.metricKey)}\""
                   for t in protocol.thresholds]

    systemInstruction = f"""
    You are a Telemetry Normalizer. Read a daily-log chat transcript and extract
    structured telemetry. Obey these rules strictly:
    1. Each reading's 'key' MUST be chosen ONLY from this exact list of keys: {metricKeys}.
       Here is what each key measures, to help you map plain language to it:
       {metricHints}
       Whenever the transcript describes the thing a key measures, add a reading with that
       key and its value (as a string, e.g. "9"). Never invent a new key.
    2. 'actions' MUST be chosen ONLY from this exact list of variable IDs: {variableIds}.
       List the IDs of items the user applied/used/combined in this entry.
    3. Put anything you noticed but could not map to the above keys into 'notes'.
    4. If a value is unclear, estimate conservatively from context (e.g. "really red" on a 1-10 scale ~ 8-9).
    """

    client = instructor.from_provider("google/gemini-2.5-flash", api_key=apiKey)
    extraction = client.create(
        model="gemini-2.5-flash",
        response_model=TelemetryExtraction,
        messages=[
            {"role": "system", "content": systemInstruction},
            {"role": "user", "content": f"Normalize this transcript: {chatTranscript}"},
        ],
        max_retries=3,
    )

    # Fold the list of readings into the dict shape the rest of the engine expects.
    metrics = {r.key: r.value for r in extraction.readings}

    # Deterministic guard: discard any metric key not declared in the protocol and
    # any action ID the protocol never declared, regardless of what the model said.
    allowedMetrics = set(metricKeys)
    allowedActions = set(variableIds)
    droppedMetrics = {k: v for k, v in metrics.items() if k not in allowedMetrics}
    cleanMetrics = {k: v for k, v in metrics.items() if k in allowedMetrics}
    cleanActions = [a for a in extraction.actions if a in allowedActions]

    notes = extraction.notes
    if droppedMetrics:
        # Record what we threw away so the trace ledger can show the normalizer drifted.
        notes = (notes or "") + f" [dropped unrecognized metrics: {list(droppedMetrics.keys())}]"

    packet = TelemetryPacket(metrics=cleanMetrics, actions=cleanActions, notes=notes)
    print(f"[runtime] collectTelemetry finished in {time.perf_counter() - startTime:.2f}s")
    return packet


# Loads the compiled protocol for an experiment from the Redis cache and parses it back
# into a ProtocolSchema. Raises if the Setup agent never ran for this experiment.
def loadProtocol(expId: str) -> ProtocolSchema:
    raw = store.getProtocol(expId)
    if raw is None:
        raise RuntimeError(f"No cached protocol for '{expId}'. Run the Setup agent first.")
    return ProtocolSchema.model_validate_json(raw)


# Runs the deterministic safety pass over a telemetry packet: every applied item is
# checked for clashes against the others in the same log, and every metric is checked
# against its threshold. Raises ProtocolViolationException listing all breaches; on a
# clean pass returns a short summary line for the trace ledger.
def evaluateSafety(packet: TelemetryPacket, protocol: ProtocolSchema) -> str:
    engine = ValidationEngine(protocol)
    violations: List[str] = []

    # Clash pass: treat the items in this one log as the active stack, and check each one
    # against the rest.
    for action in packet.actions:
        others = [a for a in packet.actions if a != action]
        ok, message = engine.validateAction(action, others)
        if not ok:
            violations.append(message)

    # Threshold pass: check every measured value against its rule.
    for metricKey, value in packet.metrics.items():
        ok, message = engine.checkThreshold(metricKey, value)
        if not ok:
            violations.append(message)

    # Drop duplicate messages while preserving order (a two-way clash can report twice).
    violations = list(dict.fromkeys(violations))
    if violations:
        raise ProtocolViolationException(violations)

    return f"{len(packet.actions)} action(s), {len(packet.metrics)} metric(s) checked; no violations"


# The Master orchestrator for one daily log. It runs its own ingest step (chat -> telemetry),
# then the deterministic safety gate; on a clean pass it writes the log to Redis, and on a
# violation it blocks the write entirely (the write line is unreachable because the safety
# gate raised). Either way it returns the PipelineTrace envelope the frontend renders.
def runTick(expId: str, transcript: str) -> PipelineTrace:
    protocol = loadProtocol(expId)
    experiment = store.getExperiment(expId)
    if experiment is None:
        raise RuntimeError(f"No experiment '{expId}' in Redis. Create it before logging.")

    stages: List[TraceStage] = []

    # Wrap the whole tick in a parent span so Phoenix shows one trace per log with the
    # ingest/safety/council stages nested under it.
    with _tracer.start_as_current_span("runTick") as rootSpan:
        rootSpan.set_attribute("expId", expId)

        # Ingest step: the Master cleans the raw chat into protocol-keyed telemetry itself.
        ingestStart = time.perf_counter()
        with _tracer.start_as_current_span("ingest") as ingestSpan:
            packet = collectTelemetry(transcript, protocol)
            ingestSummary = f"metrics {packet.metrics}, actions {packet.actions}"
            # Rich detail = what the normalizer actually pulled out of the chat.
            ingestDetail = (
                f"Input: {transcript}\n"
                f"Extracted metrics: {packet.metrics}\n"
                f"Extracted actions: {packet.actions}\n"
                f"Notes: {packet.notes or '-'}"
            )
            ingestSpan.set_attribute("summary", ingestSummary)
            ingestSpan.set_attribute("detail", ingestDetail)
        stages.append(TraceStage(
            agent="ingest",
            durationMs=(time.perf_counter() - ingestStart) * 1000,
            summary=ingestSummary,
        ))

        # Safety gate: try the deterministic checks. If they raise, we never reach the write.
        safetyStart = time.perf_counter()
        with _tracer.start_as_current_span("safety") as safetySpan:
            try:
                safetySummary = evaluateSafety(packet, protocol)
                stages.append(TraceStage(
                    agent="safety",
                    durationMs=(time.perf_counter() - safetyStart) * 1000,
                    summary=safetySummary,
                ))

                # Clean pass: persist the day's log, parking the metrics in the payload.
                log = dailyLogEntry(
                    expId=expId,
                    expStatus=experimentState(active=True),
                    payload=packet.metrics,
                    chatTranscript=transcript,
                )
                store.appendLog(log)
                verdict, violations, logStored = "ok", [], True

            except ProtocolViolationException as breach:
                stages.append(TraceStage(
                    agent="safety",
                    durationMs=(time.perf_counter() - safetyStart) * 1000,
                    summary=f"{len(breach.violations)} violation(s) - write blocked",
                ))
                verdict, violations, logStored = "blocked", breach.violations, False
            safetySpan.set_attribute("verdict", verdict)
            # Rich detail = exactly which rules were breached (or that all passed).
            safetyDetail = ("Violations:\n- " + "\n- ".join(violations)) if violations else f"Passed: {safetySummary}"
            safetySpan.set_attribute("detail", safetyDetail)

        # Council step (one LLM call): blocked entries get a calming de-escalation and a
        # forced "stop" verdict; clean entries get the arbiter's continue/adjust/stop call.
        councilStart = time.perf_counter()
        recentLogs = store.getLogs(expId)[-5:]
        context = "\n".join(f"{l.dateTime.date()}: {l.chatTranscript} | metrics={l.payload}" for l in recentLogs)
        context += f"\nLatest entry: {transcript} | metrics={packet.metrics}"

        deEscalationMessage, recoverySteps = None, []
        with _tracer.start_as_current_span("council") as councilSpan:
            if verdict == "blocked":
                de = deEscalate(violations, context)
                councilVerdict = "stop"
                deEscalationMessage, recoverySteps = de.message, de.recoverySteps
                councilSummary = "de-escalated; verdict stop"
                # Council reasoning when blocked = the de-escalator's explanation + recovery.
                councilDetail = "De-escalator: " + de.message + "\nRecovery:\n- " + "\n- ".join(de.recoverySteps)
            else:
                # Capture the arbiter's full verdict so we keep its reasoning, not just the call.
                arbiter = askVerdict(context)
                councilVerdict = arbiter.recommendation
                councilSummary = f"verdict {councilVerdict}"
                councilDetail = f"Arbiter: {arbiter.recommendation}\nReasoning: {arbiter.reason}"
            councilSpan.set_attribute("councilVerdict", councilVerdict)
            councilSpan.set_attribute("detail", councilDetail)
        stages.append(TraceStage(
            agent="council",
            durationMs=(time.perf_counter() - councilStart) * 1000,
            summary=councilSummary,
        ))

        rootSpan.set_attribute("verdict", verdict)

    return PipelineTrace(
        expId=expId,
        timestamp=datetime.now(timezone.utc).isoformat(),
        transcript=transcript,
        verdict=verdict,
        stages=stages,
        violations=violations,
        logStored=logStored,
        councilVerdict=councilVerdict,
        deEscalationMessage=deEscalationMessage,
        recoverySteps=recoverySteps,
    )
