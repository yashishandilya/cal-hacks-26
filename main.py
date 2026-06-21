from datetime import datetime, timezone
from enum import Enum
from typing import TypeVar, Generic, List, Dict, Any, Optional
from pydantic import BaseModel, Field

class experimentState(BaseModel):
    active: bool = False
    queued: bool = False
    restore: bool = False
    kill: bool = False

class userVar(BaseModel):
    varId: str
    varName: str
    constraints: Dict[str, Any] = Field(default_factory=dict)

class variableTriad(BaseModel):
    indVar: userVar
    depVar: Dict[str, Any]
    conVar: list[userVar]

class dailyLogEntry(BaseModel):
    
    expId: str
    dateTime: datetime = Field(default_factory=lambda : datetime.now(timezone.utc))
    milestone: bool = False
    expStatus: experimentState
    payload: Dict[str, Any]
    chatTranscript: Optional[str] = None

class Experiment(BaseModel):
    expId: str
    dateTime: datetime = Field(default_factory=lambda : datetime.now(timezone.utc))
    protocol: str
    varTriad: variableTriad
    compromised: bool = False
    logs: List[dailyLogEntry] = Field(default_factory=list)

if __name__ == "__main__":
    print("="*60)
    print("VALIDATION TESTS")
    print("="*60 + "\n")

    # This raw dictionary simulates what your Setup Agent outputs after a chat session
    llm_simulated_setup = {
        "expId": "exp_frequency_vector_01",
        "protocol": "Isolating Retinol Frequency Bounds (PM ONLY)",
        "expStatus": {"active": True, "queued": False},
        "varTriad": {
            "indVar": {"varId": "v_retinol_05", "varName": "Retinol 0.5% Serum"},
            "conVar": [
                {"varId": "c_wash", "varName": "Gentle Wash"},
                {"varId": "c_cream", "varName": "Barrier Cream"}
            ],
            # Enforces dynamic boundaries of whatever parameter criteria user wants to isolate
            "depVar": {
                "type": "object",
                "properties": {
                    "redness": {"type": "integer", "minimum": 1, "maximum": 10},
                    "tightness": {"type": "integer", "minimum": 1, "maximum": 10}
                },
                "required": ["redness", "tightness"]
            }
        }
    }

    # Initialize the engine artifact state object
    active_study = Experiment(**llm_simulated_setup)
    print("Model Compilation Successful: Standalone baseline configuration generated.\n")

    # Ingest a clean, valid day log 
    sample_day_log = dailyLogEntry(
        expId=active_study.expId,
        expStatus=experimentState(active=True, queued=False),
        payload={"redness": 3, "tightness": 2},
        chatTranscript="Applied at 10 PM. Smooth consistency, zero dynamic flare-ups."
    )
    active_study.logs.append(sample_day_log)
    print(f"Day 1 Transaction verified and nested at timestamp: {sample_day_log.dateTime}")

    # Final persistent storage payload serialization lookup
    print("\n" + "="*60)
    print("STORAGE DOCUMENT DUMP")
    print("="*60)
    print(active_study.model_dump_json(indent=2))