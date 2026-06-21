# The Experimenter — Technical Report

An agent-driven personal-experiment engine with a **deterministic safety core**. Users describe an experiment in plain language (e.g. "test prescription retinol, but never combine it with my AHA exfoliant, and stop if redness exceeds 7/10"); the system compiles that into a strict machine-checkable protocol, then ingests daily free-text logs, enforces safety deterministically, and provides reasoning, context compression, and full observability.

---

## 1. The core idea: LLM proposes, code disposes

The central design principle is that **LLMs are never trusted to enforce safety**. Language models do the fuzzy work they are good at — extracting structured data from messy chat, summarizing history, giving advice — but every safety-critical decision is made by a **deterministic rules engine** running plain Python, not by a model.

This shows up as defense-in-depth at three points:

1. **Protocol compilation** — the LLM drafts a rulebook, but every metric key is re-normalized *in code* to a stable `v_snake_case` ID, regardless of how the model phrased it.
2. **Telemetry ingest** — the LLM is constrained to a closed vocabulary (only the keys/IDs the protocol declares), and any key it invents anyway is *dropped in code* before validation.
3. **Safety gate** — a deterministic `ValidationEngine` (operator dispatch table + incompatibility matrix) is the sole authority on whether a log is accepted or blocked.

---

## 2. Technology stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Web framework | Flask | 3.1.3 |
| Datastore | Redis | 8.0.0 (`redis-py`) |
| LLM | Google Gemini 2.5 Flash | `google-genai` 2.9.0 |
| Structured LLM output | `instructor` | 1.15.3 |
| Schemas / validation | Pydantic | 2.13.4 |
| Observability | Arize Phoenix + OpenTelemetry | phoenix 17.9.0 / otel 1.42.1 |
| Data handling | pandas | 3.0.3 |
| Config | python-dotenv | 1.2.2 |
| Frontend | Server-rendered HTML/JS (single `index.html`) | — |

> **Note on the Anthropic track:** the reasoning committee currently runs on Gemini 2.5 Flash through `instructor`. Because everything goes through `instructor.from_provider(...)` with Pydantic response models, swapping the Researcher / De-escalator / arbiter to Claude is a one-line provider change per call — the structured-output contract is identical.

---

## 3. Architecture

```
                    ┌─────────────────────────────────────┐
   Browser UI ──────▶  Flask REST layer  (app.py)         │
                    └───────────────┬─────────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────────┐
            ▼                       ▼                           ▼
   Protocol Compiler        Master Orchestrator           Committee / Council
   (protocol_gen.py)        (runtime.py: runTick)          (council.py, compaction.py)
            │                       │                           │
            │              ingest → safety → council            │
            │                       │                           │
            ▼                       ▼                           ▼
   ┌────────────────────────────────────────────────────────────────┐
   │   Deterministic ValidationEngine (validation.py)  ← safety gate│
   └────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                  Redis store  (store.py)  ── single data-access layer
                                    │
                                    ▼
            Arize Phoenix tracing (tracing.py) — every step is a span
```

### Component responsibilities

| File | Pattern | Role |
|------|---------|------|
| `app.py` | Thin REST controllers | Maps UI actions to orchestrator calls; assembles the variable-triad DTO for the spec panel. |
| `runtime.py` | **Orchestrator + pipeline** | `runTick` runs the fixed `ingest → safety → council` pipeline and returns one `PipelineTrace` envelope. |
| `validation.py` | **Rules engine / dispatch table** | O(1) conflict check via incompatibility matrix; threshold scan via an operator→lambda dispatch table. The trust anchor. |
| `protocol_gen.py` | **Compiler** | Free-text goals → strict `ProtocolSchema` rulebook; LLM drafts, code normalizes. |
| `council.py` | **Multi-agent committee + RAG** | Researcher (Google-Search-grounded, academic-filtered, cited) + De-escalator (calm recovery guidance). |
| `compaction.py` | **Context compression + A/B eval** | Rolling-summary compaction with a quality-preservation proof. Also hosts the scientific-method arbiter. |
| `gc_agent.py` | **Lifecycle / GC** | Purge non-milestone logs on completion; hard-delete on removal. |
| `store.py` | **Repository pattern** | Sole Redis data-access layer; clean key schema, pipelined atomic writes. |
| `models.py` / `main.py` | **Schema-driven DTOs** | Pydantic contracts used for both LLM structured output and Redis (de)serialization. |
| `tracing.py` | **Observability** | Phoenix + OpenTelemetry span tree; reads spans back for the in-app trace panel. |

---

## 4. Features

### 4.1 Natural-language protocol compiler
Users state goals conversationally; `generate_dynamic_protocol` compiles them into a `ProtocolSchema` (variables, incompatibility map, safety thresholds with typed operators). Cached to Redis and written to `protocols/<expId>-rules.json` for inspection.

### 4.2 Daily-log ingest with constrained extraction
`collectTelemetry` normalizes a free-text daily log into a `TelemetryPacket` whose keys are restricted to the protocol's own vocabulary. Invented keys are dropped and recorded in `notes` so the trace shows when the model drifted.

### 4.3 Deterministic safety gate
`ValidationEngine` enforces two rule classes:
- **Incompatibilities** — O(1) lookup: is any item in this log forbidden to combine with another item in the same log?
- **Thresholds** — each metric checked against its operator (`gt/gte/lt/lte/eq/neq/contains/does not contains`).

A breach raises `ProtocolViolationException`, which **blocks the Redis write entirely** — unsafe logs never persist.

### 4.4 Reasoning committee
- **Researcher** — Gemini Google-Search grounding filtered to peer-reviewed/academic domains, returning a short summary + cited sources.
- **De-escalator** — on a blocked log, produces a calm explanation + 2–4 concrete recovery steps instead of a raw error.
- **Arbiter** — a `continue / adjust / stop` verdict over recent history.

### 4.5 Context compaction (token economy)
`compactExperiment` keeps milestones and recent logs raw, summarizes older prose, and **preserves all structured metrics exactly**. `evaluateQuality` is an A/B harness that runs the arbiter on both full and compressed context and asserts the verdict is unchanged — proving token savings without decision-quality loss.

### 4.6 Lifecycle garbage collection
On completion, non-milestone logs are purged (key events survive); on delete, all four Redis keys are removed atomically.

### 4.7 End-to-end observability
Every `runTick` is a Phoenix trace: a `runTick` root span with `ingest / safety / council` children, each carrying latency + a rich detail attribute. Traces are read back into the UI via `getExperimentTraces`.

---

## 5. Data model & Redis key design

```
exp:{expId}            → Experiment header JSON
exp:{expId}:logs       → Redis LIST of dailyLogEntry JSON (oldest → newest)
exp:{expId}:protocol   → compiled ProtocolSchema JSON (sub-ms cache)
experiments            → SET of all expIds (master index)
```

Header and logs are split so appending a log is an O(1) `RPUSH` that never rewrites the header. The protocol is cached separately for sub-millisecond reads by downstream agents.

---

## 6. Notable engineering decisions

- **List-not-dict for LLM structured output.** Gemini returns `{}` for open `{str: Any}` schemas, so extraction and incompatibility rules are requested as **lists** and folded back into dicts in code. (See `TelemetryExtraction.readings` and `IncompatibilityRule`.)
- **One trace envelope as the contract.** Every tick returns a single `PipelineTrace` (verdict, violations, council verdict, de-escalation, per-stage timing) consumed by both the UI and the observability layer.
- **Manual spans over auto-instrumentation.** The OpenInference google-genai instrumentor was incompatible with the installed `google-genai`, so spans are emitted manually — which also yields a cleaner `tick → ingest/safety/council` tree.
- **Graceful degradation for live demo.** Summarization retries on transient Gemini 503s and falls back to an extractive summary so the demo never crashes mid-run.

---

## 7. Sponsor-track mapping

| Track | Where it lives |
|-------|----------------|
| **Redis** | `store.py` — primary datastore, log lists, protocol cache, master index. |
| **Token Company** | `compaction.py` + `gc_agent.py` — context compression with measured token reduction + verdict-preservation proof. |
| **Anthropic / Claude** | Reasoning committee (`council.py`, arbiter) — currently Gemini, designed for a one-line swap to Claude. |
| **Arize** | `tracing.py` — Phoenix tracing of every agent step. |
```
