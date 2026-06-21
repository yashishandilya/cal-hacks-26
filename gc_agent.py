"""
Garbage Collector (Token Company track, storage side).

Two lifecycle actions over Redis:
- purge-non-milestone-on-complete: when an experiment ends, keep only milestone logs.
- delete-on-delete: remove an experiment and all its data (reuses store.deleteExperiment).
Compaction (the rolling-summary side) lives in compaction.py.
"""

import store


# Drops every non-milestone log for an experiment, keeping only the milestones, and
# reports how many were removed. Used when an experiment completes and the day-to-day
# noise is no longer needed but the key events must survive.
# In plain English: when an experiment is done, throw away the ordinary daily notes but
# keep the important "milestone" ones, and say how many we cleared.
def purgeNonMilestones(expId: str) -> dict:
    logs = store.getLogs(expId)
    kept = [log for log in logs if log.milestone]
    store.replaceLogs(expId, kept)
    return {"before": len(logs), "kept": len(kept), "removed": len(logs) - len(kept)}


# Marks an experiment finished by purging its non-milestone logs. Kept as its own name so
# callers express intent ("this experiment is over") rather than the mechanism.
# In plain English: end the experiment and tidy up its logs in one call.
def completeExperiment(expId: str) -> dict:
    return purgeNonMilestones(expId)


# Hard-deletes an experiment and all its data when the user deletes it. Thin wrapper over
# the store so the GC is the single place lifecycle cleanup is expressed.
# In plain English: wipe an experiment completely from the database.
def deleteExperimentData(expId: str) -> int:
    return store.deleteExperiment(expId)
