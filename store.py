"""
Redis-backed state store. Sole datastore for the experiment engine.

Key layout
----------
exp:{expId}              -> Experiment JSON (string)
exp:{expId}:logs         -> Redis LIST of dailyLogEntry JSON (oldest -> newest)
exp:{expId}:protocol     -> compiled ProtocolSchema JSON (cache for sub-ms reads)
experiments              -> SET of all known expIds (index)

Everything the Orchestrator, Garbage Collector/Compaction agent, and Council
read or write goes through this module. No other file talks to Redis directly.
"""

import os
import redis
from typing import List, Optional
from dotenv import load_dotenv

# Reuse the experiment models already defined in main.py rather than redefining.
from main import Experiment, dailyLogEntry

load_dotenv()

redisUrl = os.getenv("REDIS_URL")
if not redisUrl:
    raise RuntimeError(
        "REDIS_URL is missing. Add your Redis Essentials connection string to "
        ".env, e.g. REDIS_URL=redis://default:<password>@<host>:<port>"
    )

# decode_responses=True so every read comes back as str instead of raw bytes.
redisClient = redis.from_url(redisUrl, decode_responses=True)


# Cheap connectivity check. Called once at orchestrator startup so a bad
# REDIS_URL fails loudly up front instead of mid-pipeline.
def ping() -> bool:
    return redisClient.ping()


# Builds the key that holds the Experiment header document for an experiment.
def expKey(expId: str) -> str:
    return f"exp:{expId}"


# Builds the key that holds the Redis LIST of daily log entries.
def logsKey(expId: str) -> str:
    return f"exp:{expId}:logs"


# Builds the key that holds the cached compiled protocol JSON string.
def protocolKey(expId: str) -> str:
    return f"exp:{expId}:protocol"


# Upserts the Experiment header (everything except the logs list, which lives
# under its own key). Also registers the expId in the master 'experiments' set
# so the Garbage Collector and UI can enumerate every experiment.
def saveExperiment(exp: Experiment) -> None:
    pipe = redisClient.pipeline()
    pipe.set(expKey(exp.expId), exp.model_dump_json())
    pipe.sadd("experiments", exp.expId)
    pipe.execute()


# Fetches an Experiment by id and re-hydrates its logs from the separate list
# key onto the object. Returns None if the experiment does not exist.
def getExperiment(expId: str) -> Optional[Experiment]:
    raw = redisClient.get(expKey(expId))
    if raw is None:
        return None
    exp = Experiment.model_validate_json(raw)
    exp.logs = getLogs(expId)
    return exp


# Returns every known experiment id, sorted, from the master index set.
def listExperimentIds() -> List[str]:
    return sorted(redisClient.smembers("experiments"))


# Hard-deletes an experiment: header, logs list, cached protocol, and its entry
# in the master set. Used by the Garbage Collector on experiment delete.
# Returns how many of the three data keys actually existed and were removed.
def deleteExperiment(expId: str) -> int:
    pipe = redisClient.pipeline()
    pipe.delete(expKey(expId))
    pipe.delete(logsKey(expId))
    pipe.delete(protocolKey(expId))
    pipe.srem("experiments", expId)
    results = pipe.execute()
    return sum(results[:3])


# Appends one daily log entry to the experiment's log list. Returns the new
# total log count for that experiment.
def appendLog(entry: dailyLogEntry) -> int:
    return redisClient.rpush(logsKey(entry.expId), entry.model_dump_json())


# Returns all daily log entries for an experiment, oldest first.
def getLogs(expId: str) -> List[dailyLogEntry]:
    raw = redisClient.lrange(logsKey(expId), 0, -1)
    return [dailyLogEntry.model_validate_json(item) for item in raw]


# Atomically overwrites the entire logs list for an experiment. Used by the
# Compaction agent after folding old non-milestone logs into a summary entry,
# and by the purge-non-milestone-on-complete path.
def replaceLogs(expId: str, entries: List[dailyLogEntry]) -> None:
    pipe = redisClient.pipeline()
    pipe.delete(logsKey(expId))
    if entries:
        pipe.rpush(logsKey(expId), *[e.model_dump_json() for e in entries])
    pipe.execute()


# Caches the compiled protocol string so the Council/arbiter can read it in
# sub-ms time instead of re-reading the protocols/*.json file from disk.
def cacheProtocol(expId: str, protocolJson: str) -> None:
    redisClient.set(protocolKey(expId), protocolJson)


# Returns the cached compiled protocol JSON string, or None if not cached yet.
def getProtocol(expId: str) -> Optional[str]:
    return redisClient.get(protocolKey(expId))
