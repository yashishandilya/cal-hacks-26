"""
Flask web layer (Chunk E). Serves the Experimenter mockup and exposes JSON endpoints
that wire the UI to the Master orchestrator. The camera/environmental/sub-experiment
panels stay as static mock UI; only the journal -> runTick slice is real.
"""

import json

from flask import Flask, render_template, jsonify, request

import store
from runtime import runTick
from council import researchTopic
from compaction import compactExperiment
from protocol_gen import generate_dynamic_protocol, normalizeMetricKey
from models import ProtocolSchema
from main import Experiment, variableTriad, userVar

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/experiments")
def listExperiments():
    out = []
    for expId in store.listExperimentIds():
        exp = store.getExperiment(expId)
        out.append({
            "expId": expId,
            "protocol": exp.protocol if exp else "",
            "logCount": len(exp.logs) if exp else 0,
        })
    return jsonify(out)


@app.route("/api/experiments/<expId>/logs")
def getLogs(expId):
    logs = store.getLogs(expId)
    return jsonify([log.model_dump(mode="json") for log in logs])


# Runs one daily log through the Master orchestrator and returns the full PipelineTrace:
# verdict, violations, council verdict, de-escalation + recovery, and the stage trace.
@app.route("/api/experiments/<expId>/log", methods=["POST"])
def postLog(expId):
    transcript = (request.get_json(force=True) or {}).get("transcript", "").strip()
    if not transcript:
        return jsonify({"error": "transcript is required"}), 400
    trace = runTick(expId, transcript)
    return jsonify(trace.model_dump(mode="json"))


# Runs the Researcher on a query (defaults to the experiment's protocol topic) and returns
# real grounded findings + cited web sources for the right-hand Research sources panel.
@app.route("/api/experiments/<expId>/research", methods=["POST"])
def research(expId):
    query = (request.get_json(force=True) or {}).get("query", "").strip()
    if not query:
        exp = store.getExperiment(expId)
        query = exp.protocol if exp else expId
    return jsonify(researchTopic(query))


# Returns the compaction token stats (Token Company readout). Drops the bulky compressed
# context so the response stays small for the UI badge.
@app.route("/api/experiments/<expId>/compaction")
def compaction(expId):
    r = compactExperiment(expId)
    return jsonify({k: r[k] for k in ("tokensBefore", "tokensAfter", "reductionRatio", "logsCompacted", "logsKeptRaw")})


@app.route("/api/experiments/<expId>/protocol")
def getProtocol(expId):
    raw = store.getProtocol(expId)
    if raw is None:
        return jsonify({"error": "no protocol cached"}), 404
    return app.response_class(raw, mimetype="application/json")


# Compiles the Setup page's config cards into a real ProtocolSchema. The frontend
# assembles the hypothesis, tracked metrics, window, and committee into one transcript;
# this runs the deterministic protocol compiler (Gemini-backed), which caches the
# compiled rulebook in Redis and writes protocols/<expId>-rules.json. Returns the
# freshly cached protocol JSON so the right-hand spec panel can redraw immediately.
@app.route("/api/experiments/<expId>/compile", methods=["POST"])
def compileProtocol(expId):
    body = request.get_json(force=True) or {}
    transcript = (body.get("transcript") or "").strip()
    hypothesis = (body.get("hypothesis") or "").strip()
    # The Watch-for editor: what the user monitors (the input they change) maps to the
    # independent variable; the result they want (the outcome they measure) maps to the
    # dependent variable. 'metrics' is accepted as a legacy alias for dependent.
    independent = body.get("independent") or []
    dependent = body.get("dependent") or body.get("metrics") or []
    if not transcript:
        return jsonify({"error": "transcript is required"}), 400

    try:
        generate_dynamic_protocol(transcript, expId)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    raw = store.getProtocol(expId)
    if raw is None:
        return jsonify({"error": "compiled protocol was not cached"}), 500
    protocol = ProtocolSchema.model_validate_json(raw)

    # Rebuild the variable triad so the right-hand spec reflects the Watch-for editor.
    # The user's "what I monitor" is the independent variable, "what result I want" is the
    # dependent variable; anything they left blank falls back to the variables the protocol
    # compiler extracted.
    triad = buildVariableTriad(protocol, independent, dependent, hypothesis)

    # Persist the new triad onto the Experiment record (creating it if this is the first
    # compile). Logs live under their own Redis key, so we keep the header's logs empty.
    exp = store.getExperiment(expId)
    if exp is None:
        exp = Experiment(expId=expId, protocol=protocol.protocol, varTriad=triad)
    else:
        exp.protocol = protocol.protocol
        exp.varTriad = triad
    exp.logs = []
    store.saveExperiment(exp)
    # Note: do NOT even think of clearing the logs list here. A brand-new experiment has no logs yet (so it
    # starts empty on its own), and recompiling an existing experiment should keep its journal.

    # Live research grounded on the hypothesis, so the Research sources panel reacts too.
    # Guarded: a research failure shouldn't void an otherwise-successful compile.
    research = {"findings": "", "sources": []}
    try:
        research = researchTopic(hypothesis or protocol.protocol)
    except Exception as e:
        research = {"findings": "", "sources": [], "error": str(e)}

    return jsonify({"protocol": json.loads(raw), "research": research})


# Builds a variableTriad from the user's Watch-for editor, with the compiled protocol as
# a fallback. The user's monitored inputs become the independent variable (first one) +
# controls (the rest); the results they want become the dependent variables (scored 1-10).
# Anything left blank falls back to the variable ids the protocol compiler extracted, and
# if there is still no input we synthesize one from the hypothesis so the diagram always
# has an input node.
def buildVariableTriad(protocol: ProtocolSchema, independent, dependent, hypothesis: str) -> variableTriad:
    norm = lambda lst: [normalizeMetricKey(x) for x in (lst or []) if str(x).strip()]
    depIds = norm(dependent)
    indIds = norm(independent)

    # Every variable id the protocol mentions, in a stable order, de-duplicated (fallback).
    seen, protoVars = set(), []
    for t in protocol.thresholds:
        protoVars.append(t.metricKey)
    for key, clashes in (protocol.incompatibilities or {}).items():
        protoVars.append(key)
        protoVars.extend(clashes)
    protoVars = [v for v in protoVars if not (v in seen or seen.add(v))]

    # Dependent: user's results, else the protocol's thresholded metrics.
    if not depIds:
        depIds = [t.metricKey for t in protocol.thresholds]
    depSet = set(depIds)

    # Independent: user's monitored inputs, else the first protocol variable that isn't a
    # dependent metric. The remaining monitored inputs + any leftover protocol vars are controls.
    if not indIds:
        indIds = [v for v in protoVars if v not in depSet][:1]
    primaryInd = indIds[0] if indIds else None
    controlIds = indIds[1:] + [v for v in protoVars if v not in depSet and v not in set(indIds)]
    seenC = set()
    controlIds = [v for v in controlIds if not (v in seenC or seenC.add(v))]

    def prettyName(vid: str) -> str:
        return vid[2:].replace("_", " ").title() if vid.startswith("v_") else vid.replace("_", " ").title()

    depProps = {vid: {"type": "integer", "minimum": 1, "maximum": 10} for vid in depIds}
    depVar = {"type": "object", "properties": depProps, "required": depIds}

    if primaryInd:
        indVar = userVar(varId=primaryInd, varName=prettyName(primaryInd))
    else:
        # Fallback: name the intervention from the hypothesis so the diagram still has an input.
        snippet = " ".join(hypothesis.split()[:6]) if hypothesis else "intervention"
        indVar = userVar(varId="v_intervention", varName=snippet)
    conVar = [userVar(varId=v, varName=prettyName(v)) for v in controlIds]

    return variableTriad(indVar=indVar, depVar=depVar, conVar=conVar)


# Hard-deletes an experiment and everything attached to it: the header, its log list, and
# the cached protocol (store.deleteExperiment also drops it from the master index).
@app.route("/api/experiments/<expId>", methods=["DELETE"])
def deleteExperiment(expId):
    removed = store.deleteExperiment(expId)
    return jsonify({"deleted": expId, "keysRemoved": removed})


@app.route("/api/experiments/<expId>")
def getExperiment(expId):
    exp = store.getExperiment(expId)
    if exp is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(exp.model_dump(mode="json"))


@app.route("/api/experiments/<expId>/traces")
def traces(expId):
    import tracing
    try:
        return jsonify(tracing.getExperimentTraces(expId))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Start Phoenix tracing before the server. use_reloader=False so Phoenix isn't
    # launched twice (the reloader would spawn a second process and clash on the port).
    import tracing
    tracing.startTracing()
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
