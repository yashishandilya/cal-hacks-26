"""
Mock seeder — zero LLM calls.

Drops a fully-formed setup (Experiment record) and its compiled protocol straight
into Redis, hand-typed instead of generated, so the demo is ready instantly without
spending any Gemini calls. After running this, the ONLY thing left to do is add a
couple of daily logs (via the UI journal, or runTick("exp_retinol_mock", "...")).

This does not touch seed.py, runtime.py, or any other file — it just plants data.

Run: python mock_seed.py
"""

import json
from datetime import datetime, timedelta, timezone

import store
from models import ProtocolSchema
from main import Experiment, variableTriad, userVar, dailyLogEntry, experimentState

experimentId = "exp_retinol_mock"
protocolPath = f"protocols/{experimentId}-rules.json"


# Hand-written compiled protocol — the exact shape generate_dynamic_protocol would
# have produced for this scenario, but typed out so no model call is needed.
# The metricKeys here are what daily logs get scored against, and the incompatibilities
# map is what the clash check uses, so logs you add will actually exercise both gates.
# In plain English: the experiment's rulebook, written by hand so seeding is free.
MOCK_PROTOCOL = {
    "protocol": "Isolating Prescription Retinol 1% irritation bounds",
    "homeostasis_lockout_days": 2,
    "incompatibilities": {
        # Retinol and a peeling acid on the same night = barrier-stripping clash.
        "v_retinol_10": ["v_peeling_acid"],
        "v_peeling_acid": ["v_retinol_10"],
    },
    "thresholds": [
        {
            "metricKey": "v_redness_score",
            "operator": "gt",
            "limit": 8.0,
            "errorMessage": "Skin barrier irritation threshold crossed! Redness score above 8.",
        },
        {
            "metricKey": "v_tightness_score",
            "operator": "gt",
            "limit": 7.0,
            "errorMessage": "Tightness threshold crossed! Skin barrier likely compromised.",
        },
    ],
}


# Assembles the in-memory Experiment record, reusing the real variable-triad models so
# the stored document matches the exact shape a compiled experiment would have.
# In plain English: builds the fake-but-correctly-shaped experiment we save to Redis.
def buildMockExperiment() -> Experiment:
    return Experiment(
        expId=experimentId,
        protocol=MOCK_PROTOCOL["protocol"],
        varTriad=variableTriad(
            indVar=userVar(varId="v_retinol_10", varName="Prescription Retinol 1%"),
            conVar=[
                userVar(varId="c_wash", varName="Gentle Wash"),
                userVar(varId="c_cream", varName="Barrier Cream"),
            ],
            depVar={
                "type": "object",
                "properties": {
                    "v_redness_score": {"type": "integer", "minimum": 1, "maximum": 10},
                    "v_tightness_score": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["v_redness_score", "v_tightness_score"],
            },
        ),
    )


# A short run of pre-written daily logs, backdated day by day so the journal already
# has history when you open it. Every entry stays UNDER the thresholds (redness > 8,
# tightness > 7) and never combines the clashing actives, so they all pass clean — the
# point is to give the Council/arbiter and the Researcher real context to react to,
# while leaving the actual warning for the 1-2 logs you add live.
# In plain English: a believable week of "so far so good" entries, escalating gently, so
# when you add a hot day next the warning lands against a real backstory.
def buildMockLogs() -> list:
    # (daysAgo, redness, tightness, what the user "wrote")
    timeline = [
        (5, 2, 1, "First night on the prescription retinol. Tiny bit of stinging, barely any redness. Used the gentle wash first."),
        (4, 3, 2, "Skin felt a little tight this morning but calmed down. Redness maybe a 3. Followed with barrier cream."),
        (3, 4, 3, "Bit more red today, around a 4, and some tightness around the cheeks. Still manageable."),
        (2, 5, 4, "Noticeably pink after applying tonight, call it a 5. Tightness creeping up too. Skipped any acids."),
        (1, 6, 5, "Redness up to a 6 and my skin feels tight and a bit flaky. Watching this closely before tomorrow."),
    ]

    now = datetime.now(timezone.utc)
    logs = []
    for daysAgo, redness, tightness, transcript in timeline:
        logs.append(dailyLogEntry(
            expId=experimentId,
            dateTime=now - timedelta(days=daysAgo),
            expStatus=experimentState(active=True),
            payload={"v_redness_score": redness, "v_tightness_score": tightness},
            chatTranscript=transcript,
        ))
    return logs


# Plants the mock setup: persist the Experiment header, cache the compiled protocol in
# Redis, and mirror it to protocols/<expId>-rules.json so the on-disk folder matches.
# Leaves the log list empty on purpose — adding logs is the one step left for the user.
# In plain English: load the ready-made experiment into the database and stop there.
def main():
    print(f"Redis ping: {store.ping()}")

    protocolJson = json.dumps(MOCK_PROTOCOL, indent=2)
    # Sanity-check that the hand-typed protocol still parses as a real ProtocolSchema.
    ProtocolSchema.model_validate_json(protocolJson)

    exp = buildMockExperiment()
    store.saveExperiment(exp)
    store.cacheProtocol(experimentId, protocolJson)

    with open(protocolPath, "w") as f:
        f.write(protocolJson)

    print(f"\nSeeded mock experiment '{experimentId}' (no LLM calls).")
    print(f"  protocol      : {exp.protocol}")
    print(f"  cached rules  : {protocolPath}")
    print(f"  tracked metrics: {[t['metricKey'] for t in MOCK_PROTOCOL['thresholds']]}")
    print(f"  log count     : {len(exp.logs)}")
    print("\nReady. Add a couple of daily logs via the UI journal, or:")
    print('  from runtime import runTick')
    print(f'  runTick("{experimentId}", "Applied retinol around 10pm, skin a bit red, maybe a 4.")')


if __name__ == "__main__":
    main()
