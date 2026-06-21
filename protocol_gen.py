import os
import re
import time
import instructor
from typing import List
from pydantic import BaseModel
from google import genai
from models import ProtocolSchema, Threshold
from dotenv import load_dotenv

# Reuse the Redis store so a freshly compiled protocol can be cached for inspection.
import store


# One conflict rule expressed as a flat key/list pair. We ask the LLM for these as a
# LIST because Gemini's structured output returns {} for an open {str: [str]} dict
# (the same failure mode that left incompatibilities empty before); a list fills correctly.
class IncompatibilityRule(BaseModel):
    variableId: str
    clashesWith: List[str]


# The model-facing draft of a protocol. Identical to ProtocolSchema except the
# incompatibilities are a list of rules instead of an open dict; we convert it to a
# real ProtocolSchema (dict-shaped) in code so the ValidationEngine is unaffected.
class ProtocolDraft(BaseModel):
    protocol: str
    homeostasis_lockout_days: int
    incompatibilities: List[IncompatibilityRule]
    thresholds: List[Threshold]


# Forces a metricKey into a stable snake_case ID: lowercase, every run of non
# alphanumeric characters collapsed to one underscore, and a 'v_' prefix guaranteed
# (e.g. "clinical redness score" -> "v_clinical_redness_score").
def normalizeMetricKey(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    if not slug.startswith("v_"):
        slug = "v_" + slug
    return slug


def generate_dynamic_protocol(userTranscript: str, experimentId: str) -> str:
    startTime = time.perf_counter()
    load_dotenv()
    apiKey = os.getenv("GEMINI_API_KEY")

    if not apiKey:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it to your .env file before running the protocol generator.")

    print(f"[protocol_gen] Environment loaded in {time.perf_counter() - startTime:.2f}s")
    client = instructor.from_provider("google/gemini-2.5-flash", api_key=apiKey)
    
    systemInstruction = """
    You are a Deterministic Protocol Compiler. Translate user experimental 
    goals into the strict JSON schema provided.
    1. Extract all experimental variables as 'v_...' IDs.
    2. Normalize all conflict logic into the 'incompatibilities' map.
    3. Define safety thresholds strictly using the provided operators.
    4. Never invent keys on your own. Always use Protocol Schema.
    5. All variable names must be normalized IDs (e.g., lowercase with underscores like v_item_name).
    6. 'incompatibilities' is a LIST of rules. Add one rule for EVERY conflict the user mentions:
       each rule has 'variableId' (the item) and 'clashesWith' (the list of variable IDs it must not be combined with).
       If the user says "A cannot be used with B", add a rule {variableId: A, clashesWith: [B]}.
    7. For each item inside the thresholds list, carefully populate 'metricKey', 'operator', 'limit', and 'errorMessage'.
       'metricKey' MUST be a normalized snake_case identifier prefixed with 'v_' (lowercase, words joined by
       underscores, no spaces), e.g. 'v_redness_score'. Never use a free-text phrase with spaces.
    8. The 'operator' field MUST strictly match one of these exact ComparisonOperator values: 'gt', 'gte', 'lt', 'lte', 'eq', 'neq', 'contains', 'does not contains'.
    """

    print("[protocol_gen] Sending request to Gemini...")
    requestStartTime = time.perf_counter()
    draft = client.create(
        model="gemini-2.5-flash",
        response_model=ProtocolDraft,
        messages=[
            {"role": "system", "content": systemInstruction},
            {"role": "user", "content": f"Compile this transcript: {userTranscript}"}
        ],
        max_retries=3
    )
    print(f"[protocol_gen] Gemini response received in {time.perf_counter() - requestStartTime:.2f}s")

    # Deterministic guard: re-normalize every metricKey in code so it is a stable v_ ID
    # no matter how the model phrased it, mirroring the prompt rule above.
    for threshold in draft.thresholds:
        threshold.metricKey = normalizeMetricKey(threshold.metricKey)

    # Fold the list of conflict rules back into the flat dict the ValidationEngine reads.
    incompatibilities = {rule.variableId: rule.clashesWith for rule in draft.incompatibilities}
    protocol = ProtocolSchema(
        protocol=draft.protocol,
        homeostasis_lockout_days=draft.homeostasis_lockout_days,
        incompatibilities=incompatibilities,
        thresholds=draft.thresholds,
    )

    os.makedirs("protocols", exist_ok=True)
    file_path = f"protocols/{experimentId}-rules.json"

    protocolJson = protocol.model_dump_json(indent=2)
    with open(file_path, "w") as f:
        f.write(protocolJson)

    # Cache the compiled protocol in Redis under the experiment id so it can be
    # inspected directly (Redis Insight / CLI) and read sub-ms by later agents.
    store.cacheProtocol(experimentId, protocolJson)

    print(f"[protocol_gen] Wrote protocol JSON + cached to Redis in {time.perf_counter() - startTime:.2f}s total")

    return file_path