"""
Seed script for a Redis test database.

Generates a synthetic daily-log transcript with Gemini, stores a test experiment
+ its log into Redis, runs the ingest step (collectTelemetry), prints the
resulting TelemetryPacket, and reads everything back out of Redis to prove the
test database is really there.

Run: python seed.py
"""

import os
import instructor
from pydantic import BaseModel
from dotenv import load_dotenv

import store
from models import ProtocolSchema
from runtime import collectTelemetry

# Reuse the experiment/log models already defined in main.py.
from main import Experiment, dailyLogEntry, experimentState, variableTriad, userVar

protocolPath = "protocols/exp_skin_999-rules.json"
experimentId = "exp_skin_999"


# Tiny wrapper schema so Gemini's structured output returns one clean string field
# instead of an open object (open dicts come back empty from Gemini, see runtime.py).
class GeneratedTranscript(BaseModel):
    transcript: str


# Asks Gemini to write a believable, messy daily-log chat message for the given
# scenario, the kind of thing a real user would type into the app at night.
def generateTranscript(scenario: str) -> str:
    load_dotenv()
    apiKey = os.getenv("GEMINI_API_KEY")
    if not apiKey:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it to your .env file before seeding.")

    client = instructor.from_provider("google/gemini-2.5-flash", api_key=apiKey)
    result = client.create(
        model="gemini-2.5-flash",
        response_model=GeneratedTranscript,
        messages=[
            {"role": "system", "content": (
                "You write short, realistic, slightly messy daily-log chat messages as if a "
                "real person were journaling about their experiment day. 2-4 sentences, casual tone."
            )},
            {"role": "user", "content": f"Write one daily-log message for this scenario: {scenario}"},
        ],
        max_retries=3,
    )
    return result.transcript


# Builds the in-memory test Experiment object for the skincare retinol study,
# reusing the variable-triad models so the stored document matches real shape.
def buildTestExperiment() -> Experiment:
    return Experiment(
        expId=experimentId,
        protocol="Isolating Prescription Retinol 1% irritation bounds",
        varTriad=variableTriad(
            indVar=userVar(varId="v_retinol_10", varName="Prescription Retinol 1%"),
            conVar=[userVar(varId="c_wash", varName="Gentle Wash")],
            depVar={
                "type": "object",
                "properties": {"v_redness_score": {"type": "integer", "minimum": 1, "maximum": 10}},
                "required": ["v_redness_score"],
            },
        ),
    )


# Runs the full seed flow: persist experiment + cached protocol, generate a log,
# ingest it into a TelemetryPacket, store the log, then read it all back from Redis.
def main():
    print("=" * 60)
    print("SEEDING REDIS TEST DATABASE")
    print("=" * 60)

    # Confirm the connection up front so a bad REDIS_URL fails loudly.
    print(f"Redis ping: {store.ping()}")

    # Load the compiled protocol the Setup agent already produced, and cache it in
    # Redis so later reads are sub-ms instead of hitting disk (the Redis-track win).
    with open(protocolPath) as f:
        protocolJson = f.read()
    protocol = ProtocolSchema.model_validate_json(protocolJson)

    exp = buildTestExperiment()
    store.saveExperiment(exp)
    store.cacheProtocol(experimentId, protocolJson)
    print(f"\nSaved experiment '{experimentId}' and cached its protocol.")

    # Generate a realistic daily-log transcript that should describe high redness.
    scenario = ("Night skincare log: applied prescription retinol around 10pm. Skin got "
                "quite red and irritated afterward, around a 9 out of 10.")
    transcript = generateTranscript(scenario)
    print(f"\nGenerated transcript:\n  {transcript}")

    # Ingest the transcript into a key-matched TelemetryPacket.
    packet = collectTelemetry(transcript, protocol)
    print("\nTelemetryPacket:")
    print(f"  metrics: {packet.metrics}")
    print(f"  actions: {packet.actions}")
    print(f"  notes  : {packet.notes}")

    # Store the day's log, parking the extracted metrics in the payload and keeping
    # the raw chat alongside it for later compaction.
    log = dailyLogEntry(
        expId=experimentId,
        expStatus=experimentState(active=True),
        payload=packet.metrics,
        chatTranscript=transcript,
    )
    logCount = store.appendLog(log)
    print(f"\nStored daily log. Experiment now has {logCount} log(s).")

    # Read everything back out of Redis to prove the test database persisted.
    print("\n" + "=" * 60)
    print("READBACK FROM REDIS")
    print("=" * 60)
    fetched = store.getExperiment(experimentId)
    print(f"Experiment ids in Redis: {store.listExperimentIds()}")
    print(f"Fetched protocol title : {fetched.protocol}")
    print(f"Fetched log count       : {len(fetched.logs)}")
    print(f"First log payload       : {fetched.logs[0].payload}")
    print(f"First log transcript    : {fetched.logs[0].chatTranscript}")


if __name__ == "__main__":
    main()
