from enum import Enum
from datetime import datetime, timezone
from typing import TypeVar, Generic, List, Dict, Any, Optional, Union
from pydantic import BaseModel, Field

class ComparisonOperator(str, Enum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    NOT_EQ = "neq"
    CONTAINS = "contains" 
    NO_CONTAINS = "does not contains"


class Threshold(BaseModel):
    metricKey: str
    operator: str
    limit: "Union[float, str]" # Allows for numeric or categorical comparisons
    errorMessage: str

class ProtocolSchema(BaseModel):
    protocol: str
    homeostasis_lockout_days: int
    incompatibilities: Dict[str, List[str]] # e.g., {"v_a": ["v_b", "v_c"]}
    thresholds: List[Threshold]


# Normalized output of the Orchestrator's ingest step. The normalization call is
# handed the protocol's own metricKeys / variable IDs, so it can ONLY emit keys
# that already exist in the protocol. This closes the seam where free-text chat
# logs produced keys that never matched the protocol and silently passed safety.
class TelemetryPacket(BaseModel):
    metrics: Dict[str, Any]          # metricKey -> measured value, keyed to the protocol
    actions: List[str]               # variable IDs applied/used in this log entry
    notes: Optional[str] = None      # anything the normalizer couldn't map to a key


# Raised by the Orchestrator when the deterministic ValidationEngine flags a clash
# or a breached threshold. Carries the list of human-readable violation messages so
# the trace envelope and the UI banner can surface exactly what tripped, and so the
# Orchestrator can block the Redis write when this is raised.
class ProtocolViolationException(Exception):
    def __init__(self, violations: List[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


# One step in the Master orchestrator's run, recorded for the UI trace ledger:
# which sub-step ran, how long it took, and a short human-readable result line.
class TraceStage(BaseModel):
    agent: str               # name of the step, e.g. "ingest" or "safety"
    durationMs: float        # how long this step took, in milliseconds
    summary: str             # short readable description of what this step produced


# The full envelope the Master orchestrator returns for one log. The frontend reads
# 'verdict' + 'violations' for the warning banner and 'stages' for the trace ledger.
class PipelineTrace(BaseModel):
    expId: str
    timestamp: str                              # ISO timestamp of the run
    transcript: str                             # the raw log text that was processed
    verdict: str                                # "ok" if accepted, "blocked" if a violation stopped it
    stages: List[TraceStage] = Field(default_factory=list)
    violations: List[str] = Field(default_factory=list)
    logStored: bool = False                     # whether the log was actually written to Redis
    # Council outputs (added after the deterministic gate). councilVerdict is the arbiter's
    # continue/adjust/stop call (distinct from verdict above, which is ok/blocked); the
    # de-escalation fields are populated only when blocked.
    councilVerdict: Optional[str] = None
    deEscalationMessage: Optional[str] = None
    recoverySteps: List[str] = Field(default_factory=list)

    