"""
Committee / Council agents (running on Gemini for now).

D1: Researcher - uses Gemini's Google Search grounding to pull real, cited sources
that ground the committee's reasoning. Feeds the UI "research sources" panel.
"""

import os
import instructor
from typing import List
from pydantic import BaseModel
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
geminiClient = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


# Domains we count as academic/peer-reviewed: literature indexes, journal publishers, and
# university (.edu / .ac.) sites. The grounding chunk's title is the source domain (e.g.
# "ncbi.nlm.nih.gov"), so a substring check against this list filters out blogs and
# product/marketing pages. In plain English: the allow-list of "real research" websites.
ACADEMIC_DOMAINS = (
    "ncbi.nlm.nih.gov", "pubmed", "pmc", "doi.org", ".edu", ".ac.", "nature.com",
    "sciencedirect", "springer", "wiley", "onlinelibrary", "tandfonline", "sagepub",
    "jamanetwork", "nejm.org", "thelancet", "bmj.com", "cell.com", "frontiersin",
    "mdpi.com", "plos.org", "cochrane", "researchgate", "semanticscholar", "scholar.google",
    "oup.com", "cambridge.org", "annualreviews", "karger", "dovepress", "jaad.org", "elsevier",
)


# True if a grounded source looks academic (its domain matches the allow-list above).
# In plain English: decides whether a cited link is from real research, not a blog or shop.
def isAcademicSource(title: str, url: str) -> bool:
    haystack = f"{title or ''} {url or ''}".lower()
    return any(domain in haystack for domain in ACADEMIC_DOMAINS)


# Researches an experiment topic with Gemini's Google Search grounding, returning a short
# findings summary plus only the peer-reviewed/academic sources Gemini cited (title + url).
# In plain English: searches scientific literature for real and hands back what it found
# plus the research links (filtering out blogs and product pages).
def researchTopic(query: str) -> dict:
    prompt = (
        "Research this experiment topic using ONLY peer-reviewed academic and scientific "
        "sources: journal articles, clinical trials, systematic reviews, PubMed/PMC entries, "
        "and university (.edu) publications. Do NOT use blogs, news articles, product pages, "
        "or commercial/marketing sites. Give a brief, factual 2-3 sentence summary of what "
        f"the peer-reviewed evidence says. Topic: {query}"
    )
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    )
    response = geminiClient.models.generate_content(
        model="gemini-2.5-flash", contents=prompt, config=config
    )

    findings = (response.text or "").strip()

    # Pull cited sources from the grounding metadata, then keep only academic ones. Guards
    # for when Gemini returns no grounding at all (then we keep findings + an empty list).
    # In plain English: dig out the links it used and drop anything that isn't real research.
    sources: List[dict] = []
    candidate = response.candidates[0] if response.candidates else None
    metadata = getattr(candidate, "grounding_metadata", None) if candidate else None
    chunks = getattr(metadata, "grounding_chunks", None) if metadata else None
    if chunks:
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            if web and isAcademicSource(web.title, web.uri):
                sources.append({"title": web.title, "url": web.uri})

    return {"query": query, "findings": findings, "sources": sources}


# The De-escalator's output: a calm, plain-language explanation of what tripped and a
# few concrete recovery steps. Maps to the UI's red "De-escalator triggered" flag box.
# In plain English: a reassuring note about what went wrong and what to do about it.
class DeEscalation(BaseModel):
    message: str               # calm explanation of what happened and why it matters
    recoverySteps: List[str]   # 2-4 concrete steps to recover safely


# Given the safety violations (and optional context), produces a supportive de-escalation
# instead of a raw error. Runs only after the deterministic gate has already blocked.
# In plain English: when something unsafe is caught, this explains it kindly and says how
# to recover, rather than just throwing a scary error.
def deEscalate(violations: List[str], context: str = "") -> DeEscalation:
    client = instructor.from_provider("google/gemini-2.5-flash", api_key=os.getenv("GEMINI_API_KEY"))
    joined = "; ".join(violations)
    return client.create(
        model="gemini-2.5-flash",
        response_model=DeEscalation,
        messages=[
            {"role": "system", "content": (
                "You are a calm, reassuring safety de-escalator for a personal-experiment app. A "
                "protocol violation was detected and the action was blocked. Explain plainly and "
                "without alarm what happened and why it matters, then give 2-4 concrete recovery "
                "steps. Be supportive, never preachy."
            )},
            {"role": "user", "content": f"Violations: {joined}\nContext: {context}"},
        ],
        max_retries=3,
    )
