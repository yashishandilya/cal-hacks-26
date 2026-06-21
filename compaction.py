"""
Compaction agent (Token Company track).

Compresses an experiment's log history so an LLM sees far fewer tokens while the
decision-relevant signal is preserved. v1 is non-destructive: it computes the
compressed context and the token savings; it does NOT overwrite Redis (that is the
Garbage Collector's job in Chunk C).
"""

import os
import time
import instructor
from typing import List, Literal
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv

import store
from main import dailyLogEntry

load_dotenv()
geminiClient = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


# Counts tokens in a string using Gemini's own tokenizer (the same model family that
# reads the context downstream), so the "tokens saved" figure reflects real LLM cost.
def countTokens(text: str) -> int:
    if not text:
        return 0
    result = geminiClient.models.count_tokens(model="gemini-2.5-flash", contents=text)
    return result.total_tokens


def renderLog(log: dailyLogEntry) -> str:
    tag = " [milestone]" if log.milestone else ""
    return f"{log.dateTime.date()}{tag}: {log.chatTranscript} | metrics={log.payload}"


# Folds several logs' prose into a short factual summary with one LLM call. The
# structured metrics are preserved separately, so only the verbose narrative is lost.
def summarizeLogs(logs: List[dailyLogEntry]) -> str:
    joined = "\n".join(renderLog(l) for l in logs)
    prompt = (
        "Summarize these daily experiment logs into 2-3 factual sentences capturing the "
        "overall trend and any notable events. Do not invent numbers.\n\n" + joined
    )
    # Retry a few times on transient errors (Gemini 503 spikes), then fall back to a
    # trivial extractive summary so a live demo never crashes on a bad moment.
    for attempt in range(3):
        try:
            response = geminiClient.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            return response.text.strip()
        except Exception:
            time.sleep(2)
    return f"[fallback] {len(logs)} earlier logs compacted; metrics preserved below."


# Compacts an experiment's history: milestones and the most recent `keepRecent` logs stay
# raw, the rest have their prose summarized while their metrics are preserved exactly.
# Non-destructive (Redis is untouched); returns the token savings and compressed context.
def compactExperiment(expId: str, keepRecent: int = 3) -> dict:
    logs = store.getLogs(expId)
    recentCutoff = len(logs) - keepRecent

    keptRaw: List[dailyLogEntry] = []
    toCompress: List[dailyLogEntry] = []
    for i, log in enumerate(logs):
        if log.milestone or i >= recentCutoff:
            keptRaw.append(log)
        else:
            toCompress.append(log)

    fullContext = "\n".join(renderLog(l) for l in logs)

    parts: List[str] = []
    if toCompress:
        summary = summarizeLogs(toCompress)
        preservedMetrics = [l.payload for l in toCompress]
        parts.append(f"[compacted {len(toCompress)} earlier logs] {summary}")
        parts.append(f"[preserved metrics] {preservedMetrics}")
    parts.extend(renderLog(l) for l in keptRaw)
    compressedContext = "\n".join(parts)

    before = countTokens(fullContext)
    after = countTokens(compressedContext)
    return {
        "tokensBefore": before,
        "tokensAfter": after,
        "reductionRatio": round(1 - after / before, 3) if before else 0.0,
        "logsCompacted": len(toCompress),
        "logsKeptRaw": len(keptRaw),
        "compressedContext": compressedContext,
    }


def compactionReport(expId: str, keepRecent: int = 3) -> str:
    r = compactExperiment(expId, keepRecent)
    saved = r["tokensBefore"] - r["tokensAfter"]
    return "\n".join([
        "=" * 48,
        f"COMPACTION REPORT - {expId}",
        "=" * 48,
        f"Logs compacted : {r['logsCompacted']}",
        f"Logs kept raw  : {r['logsKeptRaw']} (milestones + recent {keepRecent})",
        f"Tokens before  : {r['tokensBefore']}",
        f"Tokens after   : {r['tokensAfter']}",
        f"Tokens saved   : {saved}  ({r['reductionRatio']*100:.1f}% reduction)",
        "=" * 48,
    ])


# A single decision the committee would make from an experiment's history: whether to
# continue, adjust, or stop, plus a one-line reason. Constrained so the A/B test compares
# a clean categorical verdict rather than free prose.
class DecisionVerdict(BaseModel):
    recommendation: Literal["continue", "adjust", "stop"]
    reason: str


def askVerdict(context: str) -> DecisionVerdict:
    client = instructor.from_provider("google/gemini-2.5-flash", api_key=os.getenv("GEMINI_API_KEY"))
    return client.create(
        model="gemini-2.5-flash",
        response_model=DecisionVerdict,
        messages=[
            {"role": "system", "content": (
                "You are a cautious experiment advisor. Given the experiment history, decide whether "
                "the user should continue, adjust, or stop, based mainly on the redness trend and any "
                "safety events. Be consistent and deterministic."
            )},
            {"role": "user", "content": context},
        ],
        max_retries=3,
    )


# The quality-preservation proof for the Token Company track: run the same decision on the
# FULL history and on the COMPRESSED context, and check the verdict is unchanged. If they
# agree, we preserved decision quality while spending far fewer tokens.
def evaluateQuality(expId: str, keepRecent: int = 3) -> dict:
    logs = store.getLogs(expId)
    fullContext = "\n".join(renderLog(l) for l in logs)
    compaction = compactExperiment(expId, keepRecent)
    compressedContext = compaction["compressedContext"]

    verdictFull = askVerdict(fullContext)
    verdictCompressed = askVerdict(compressedContext)
    agree = verdictFull.recommendation == verdictCompressed.recommendation

    return {
        "agree": agree,
        "verdictFull": verdictFull.recommendation,
        "verdictCompressed": verdictCompressed.recommendation,
        "reasonFull": verdictFull.reason,
        "reasonCompressed": verdictCompressed.reason,
        "tokensBefore": compaction["tokensBefore"],
        "tokensAfter": compaction["tokensAfter"],
        "reductionRatio": compaction["reductionRatio"],
    }
