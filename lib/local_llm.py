"""local_llm.py — thin OpenAI-compatible client to a LOCAL open-weight model.

Two jobs in the pipeline, both on a model OUTSIDE the Claude family:

  1. RETRIEVAL TIER — condense raw web search/fetch results into compact, *quoted*
     structured evidence notes, so raw web pages never enter Claude context. This is
     the cost unlock: raw pages are 10x-50x the size of the notes (AgentDiet 2025).
  2. ADVERSARIAL CRITIC — a blind, rubric-anchored critic in the post-mortem panel.
     A different model family kills the self-preference bias that single-agent
     self-grading carries (Verga 2024 PoLL; Wataoka NeurIPS'24).

Speaks OpenAI-compatible ``/chat/completions`` (Ollama, vLLM, LM Studio, or any
hosted gateway). Pure stdlib (``urllib``) — no SDK dependency, so it runs in the
same minimal environment as the rest of the pipeline.

Every network call degrades gracefully: ``ping()`` lets the orchestrator detect a
down endpoint and fall back to a Sonnet retrieval/critic agent, so the pipeline
never hard-breaks on a stopped local server.

Public API
----------
ping(timeout=...) -> bool
chat(messages, *, model, temperature, max_tokens) -> str
complete_json(system, user, *, max_tokens) -> dict
extract_evidence(question, search_results) -> dict   # structured EvidenceNotes
critique(question, forecast_prob, reasoning, outcome, market_implied, rubric) -> dict
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from . import config


class LocalLLMError(RuntimeError):
    """Any failure talking to / parsing the local model (caller should fall back)."""


# ---------------------------------------------------------------------------
# Transport (OpenAI-compatible, stdlib only)
# ---------------------------------------------------------------------------

def _url(path: str) -> str:
    return f"{config.LOCAL_LLM_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _base_payload(messages: list[dict], *, model: Optional[str], temperature: float,
                  max_tokens: int) -> dict:
    """Build the chat payload, injecting thinking-suppression so a hybrid reasoning model
    (Qwen3) cannot burn the output budget on a <think> block and return truncated JSON.
    ``reasoning_effort="none"`` is the field Ollama's OpenAI endpoint honors;
    ``chat_template_kwargs`` is the vLLM equivalent (ignored elsewhere). Both are harmless
    to non-reasoning models (Mistral)."""
    payload: dict = {
        "model": model or config.LOCAL_LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if config.LOCAL_LLM_SUPPRESS_THINKING:
        payload["reasoning_effort"] = "none"
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _post(path: str, payload: dict, timeout: Optional[float] = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _url(path),
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LOCAL_LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.LOCAL_LLM_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise LocalLLMError(f"local LLM POST {path} failed: {e}") from e
    except json.JSONDecodeError as e:
        raise LocalLLMError(f"local LLM returned non-JSON body: {e}") from e


def ping(timeout: float = 5.0) -> bool:
    """True if the endpoint answers ``GET /models`` with HTTP 200.

    Never raises — a down server simply returns False so callers fall back."""
    req = urllib.request.Request(
        _url("models"),
        headers={"Authorization": f"Bearer {config.LOCAL_LLM_API_KEY}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def chat(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: Optional[float] = None,
) -> str:
    """Single chat completion -> assistant text. Raises LocalLLMError on transport
    failure or a malformed response shape."""
    payload = _base_payload(messages, model=model, temperature=temperature, max_tokens=max_tokens)
    resp = _post("chat/completions", payload, timeout=timeout)
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LocalLLMError(f"unexpected chat response shape: {resp!r}") from e


def chat_tools(
    messages: list[dict],
    *,
    tools: Optional[list[dict]] = None,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    timeout: Optional[float] = None,
) -> dict:
    """Chat completion that may return tool calls. Returns the raw assistant *message*
    dict (``{"content": str, "tool_calls": [...] }``) so the caller can run an agentic
    tool-use loop (see ``lib.retrieval.gather_evidence``). Passing ``tools=None`` forces a
    plain text turn. Raises LocalLLMError on transport / shape failure."""
    payload = _base_payload(messages, model=model, temperature=temperature, max_tokens=max_tokens)
    if tools:
        payload["tools"] = tools
    resp = _post("chat/completions", payload, timeout=timeout)
    try:
        return resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LocalLLMError(f"unexpected chat_tools response shape: {resp!r}") from e


def _extract_json_block(text: str) -> dict:
    """Parse a JSON object out of model text, tolerating ```json fences / prose.

    Small open-weight models often wrap JSON in markdown or add a preamble; we take
    the outermost {...} span and parse it."""
    s = text.strip()
    if s.startswith("```"):
        # strip a leading ```json / ``` fence and trailing ```
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LocalLLMError(f"no JSON object found in model output: {text[:200]!r}")
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        raise LocalLLMError(f"could not parse JSON object: {e}") from e


def complete_json(system: str, user: str, *, max_tokens: int = 1024,
                  temperature: float = 0.0, model: Optional[str] = None,
                  retries: int = 1) -> dict:
    """Chat with a JSON-only instruction and return the parsed object.

    Retries once by default on an unparseable/truncated body (with thinking suppressed
    these are rare, but a single retry absorbs the occasional malformed pass instead of
    dropping it — what used to silently shrink the forecast ensemble)."""
    messages = [
        {"role": "system", "content": system +
         "\n\nRespond with ONE valid JSON object and nothing else. No prose, no markdown."},
        {"role": "user", "content": user},
    ]
    last: Optional[LocalLLMError] = None
    for _ in range(max(1, retries + 1)):
        try:
            return _extract_json_block(
                chat(messages, max_tokens=max_tokens, temperature=temperature, model=model))
        except LocalLLMError as e:
            last = e
    raise last if last else LocalLLMError("complete_json: exhausted retries")


# ---------------------------------------------------------------------------
# Retrieval tier — structured evidence notes
# ---------------------------------------------------------------------------

_EVIDENCE_SYSTEM = (
    "You are a research condenser for a superforecaster. Given a forecasting "
    "question and raw web search results, extract the decision-relevant facts into "
    "compact structured notes. Include a SHORT verbatim quote for each fact so a "
    "downstream analyst can verify it without re-reading the page. Do not forecast; "
    "do not editorialize. Prefer primary sources and recent dates."
)

_EVIDENCE_SCHEMA_HINT = (
    'JSON shape: {"question": str, "as_of": str, '
    '"facts": [{"claim": str, "quote": str, "source": str, "date": str, '
    '"supports": "yes"|"no"|"unclear"}], '
    '"base_rates": [str], "key_uncertainties": [str]}'
)


def extract_evidence(question: str, search_results: str, *, as_of: str = "") -> dict:
    """Condense raw web ``search_results`` into structured, quoted evidence notes.

    Returns a dict (see ``_EVIDENCE_SCHEMA_HINT``). Raises LocalLLMError if the model
    is unreachable or returns unparseable output — the caller falls back to Sonnet."""
    user = (
        f"QUESTION: {question}\n"
        f"AS_OF: {as_of}\n\n"
        f"OUTPUT {_EVIDENCE_SCHEMA_HINT}\n\n"
        f"RAW SEARCH RESULTS:\n{search_results}"
    )
    notes = complete_json(_EVIDENCE_SYSTEM, user, max_tokens=4000)  # free + unmetered; generous headroom
    notes.setdefault("question", question)
    notes.setdefault("as_of", as_of)
    notes.setdefault("facts", [])
    return notes


# ---------------------------------------------------------------------------
# Adversarial critic — blind, rubric-anchored post-mortem
# ---------------------------------------------------------------------------

_CRITIC_SYSTEM = (
    "You are an adversarial forecasting critic. You are given a resolved question, a "
    "forecaster's probability and reasoning (the forecaster's IDENTITY is hidden from "
    "you), the actual outcome, and the market-implied probability at forecast time. "
    "Judge ONLY the reasoning quality against the fixed rubric — not whether the "
    "outcome happened (a good forecast can still lose; a bad one can still win). Be "
    "skeptical and specific. For each rubric item return pass=true/false with a one-"
    "line reason."
)


def critique(
    question: str,
    forecast_prob: float,
    reasoning: str,
    outcome: int,
    market_implied: Optional[float],
    rubric: tuple[str, ...] = config.POSTMORTEM_RUBRIC,
) -> dict:
    """Run the blind rubric critique on the LOCAL model. Returns
    ``{"rubric_scores": {item: {"pass": bool, "reason": str}}, "summary": str,
    "biggest_miss": str}``. Raises LocalLLMError so the caller can fall back."""
    rubric_list = "\n".join(f"- {item}" for item in rubric)
    user = (
        f"QUESTION: {question}\n"
        f"FORECAST PROBABILITY (YES): {forecast_prob}\n"
        f"MARKET-IMPLIED AT FORECAST TIME: "
        f"{'n/a' if market_implied is None else market_implied}\n"
        f"ACTUAL OUTCOME: {'YES' if outcome == 1 else 'NO'}\n\n"
        f"FORECASTER REASONING:\n{reasoning}\n\n"
        f"RUBRIC (score each):\n{rubric_list}\n\n"
        'OUTPUT JSON: {"rubric_scores": {"<item>": {"pass": bool, "reason": str}}, '
        '"summary": str, "biggest_miss": str}'
    )
    verdict = complete_json(_CRITIC_SYSTEM, user, max_tokens=3000)  # free + unmetered; generous headroom
    verdict.setdefault("rubric_scores", {})
    verdict.setdefault("summary", "")
    return verdict


# ---------------------------------------------------------------------------
# Forecasting tier — LOCAL model (Qwen) is the FORECASTER for every market.
# PROJECT DIRECTIVE (model routing, 2026-06-23): Qwen does EVERYTHING — retrieval
# condensation, forecasting, and adversarial gating. No Anthropic models form forecasts.
# High confidence is *earned* by running an ENSEMBLE of independent passes (see
# forecast_ensemble): tight agreement -> high, wide spread -> low. This recovers the
# calibration a single small model loses and is what lets a lean clear the
# min_confidence_for_lean floor without weakening any risk gate.
# ---------------------------------------------------------------------------

_FORECASTER_SYSTEM = (
    "You are a calibrated superforecaster for human-behavior markets (politics, policy, "
    "culture). Reason from the supplied evidence notes ONLY. Follow the method: establish a "
    "base rate / reference class first, update incrementally on the evidence, run a quick "
    "pre-mortem, and avoid overreacting to a single noisy signal. Output a GRANULAR probability "
    "(not lazy round numbers). You are NOT given the market price — do not guess it. Judge "
    "epistemic confidence separately from the probability."
)


def forecast(question: str, evidence_notes: dict, *, as_of: str = "",
             temperature: float = 0.0, model: Optional[str] = None) -> dict:
    """LOCAL-model forecaster — ONE pass. Returns
    ``{"my_probability": float, "my_confidence": "low|medium|high",
    "rationale_summary": str, "key_drivers": [str], "reference_classes": [str]}``.
    Raises LocalLLMError on transport/parse failure. For a calibrated estimate use
    ``forecast_ensemble`` (multiple passes at temperature>0). ``model`` selects the
    forecaster (defaults to ``config.LOCAL_LLM_MODEL``); the arm passes its model tag so
    different local models compete on the scoreboard."""
    import json as _json
    # Thinking is suppressed centrally (config.LOCAL_LLM_SUPPRESS_THINKING -> reasoning_effort
    # "none"), so a hybrid reasoning model can't blow the budget on a <think> block. The
    # budget below is generous truncation headroom (free + unmetered), not a cost cap.
    user = (
        f"QUESTION: {question}\n"
        f"AS_OF: {as_of}\n\n"
        f"EVIDENCE NOTES (JSON):\n{_json.dumps(evidence_notes, indent=2)}\n\n"
        'OUTPUT JSON: {"my_probability": <0-1 float>, "my_confidence": "low|medium|high", '
        '"rationale_summary": str, "key_drivers": [str], "reference_classes": [str]}'
    )
    out = complete_json(_FORECASTER_SYSTEM, user, max_tokens=3072, temperature=temperature,
                        model=model)
    # Clamp + validate the probability so a malformed value can't poison the record.
    p = out.get("my_probability")
    if not isinstance(p, (int, float)) or not (0.0 <= float(p) <= 1.0):
        raise LocalLLMError(f"local forecaster returned invalid probability: {p!r}")
    out["my_probability"] = float(p)
    out.setdefault("my_confidence", "low")
    return out


def forecast_ensemble(question: str, evidence_notes: dict, *, n: int = 5,
                      as_of: str = "", temperature: float = 0.7,
                      n_sources: Optional[int] = None, model: Optional[str] = None) -> dict:
    """Run ``n`` INDEPENDENT Qwen forecasts (temperature>0 for diversity) and fuse them.

    Confidence is EARNED from agreement, not asserted by one small model:
      - spread_confidence: pstdev(probs) <=0.05 -> high, <=0.12 -> medium, else low.
      - if ``n_sources`` is given, a thin evidence base (<5 disparate sources) caps
        confidence at 'medium' (the >5-disparate-sources rule).
    Returns ``{"probs": [...], "n": int, "n_requested": int, "mean", "median",
    "stdev", "my_probability" (=median, robust to an outlier pass), "my_confidence",
    "spread_confidence", "rationale_summary", "key_drivers", "reference_classes",
    "samples": [...]}``. Raises LocalLLMError only if EVERY pass fails."""
    import statistics
    probs: list[float] = []
    samples: list[dict] = []
    for i in range(max(1, n)):
        try:
            # Vary temperature slightly per pass to avoid collapsed/degenerate agreement.
            out = forecast(question, evidence_notes, as_of=as_of,
                           temperature=round(temperature + 0.05 * (i % 3), 3), model=model)
        except LocalLLMError:
            continue
        probs.append(out["my_probability"])
        samples.append(out)
    if not probs:
        raise LocalLLMError("forecast_ensemble: every pass failed")

    mean = statistics.fmean(probs)
    median = statistics.median(probs)
    stdev = statistics.pstdev(probs) if len(probs) > 1 else 0.0
    if stdev <= 0.05:
        spread_conf = "high"
    elif stdev <= 0.12:
        spread_conf = "medium"
    else:
        spread_conf = "low"

    confidence = spread_conf
    # A single pass cannot earn 'high' (no agreement signal yet).
    if len(probs) < 3 and confidence == "high":
        confidence = "medium"
    # The >5-disparate-sources rule: a thin evidence base caps confidence at medium.
    if n_sources is not None and n_sources < 5 and confidence == "high":
        confidence = "medium"

    best = max(samples, key=lambda s: len(str(s.get("rationale_summary", ""))))
    return {
        "probs": probs,
        "n": len(probs),
        "n_requested": max(1, n),
        "mean": round(mean, 4),
        "median": round(median, 4),
        "stdev": round(stdev, 4),
        "my_probability": round(median, 4),  # median is robust to one rogue pass
        "my_confidence": confidence,
        "spread_confidence": spread_conf,
        "rationale_summary": best.get("rationale_summary", ""),
        "key_drivers": best.get("key_drivers", []),
        "reference_classes": best.get("reference_classes", []),
        "samples": samples,
    }


# ---------------------------------------------------------------------------
# Adversarial DECISION GATE — challenge a proposed position BEFORE it is committed.
# A single agent (even Opus) cannot be trusted to grade its own decision; this is the
# independent, different-model-family check that runs IN the loop, not post-mortem.
# ---------------------------------------------------------------------------

_CHALLENGE_SYSTEM = (
    "You are an ADVERSARIAL risk reviewer guarding a paper-trading book. A forecaster — a "
    "different, larger model — has proposed a probability and a position. Your job is NOT to "
    "agree; it is to find every reason the position is WRONG before capital is committed. A "
    "single agent cannot be trusted to check its own decision: you are that independent check.\n"
    "Attack on these axes:\n"
    "- EDGE REALITY: is the gap vs the market a true edge, or noise the forecaster is fooling "
    "itself with? If the position diverges far from a LIQUID market, the default is that the "
    "forecaster is missing something the crowd knows — demand a specific, credible reason.\n"
    "- OVERCONFIDENCE: is the stated confidence justified by the evidence, or inflated?\n"
    "- REASONING FLAWS: base-rate neglect, over-update on a vivid detail, one-sided evidence, "
    "stale or thin sourcing.\n"
    "Return VETO (do not take it), REVISE (take a smaller/adjusted position), or CONFIRM (it "
    "survives scrutiny). Default toward skepticism; CONFIRM only what genuinely withstands attack."
)


def challenge(
    question: str,
    proposed_probability: float,
    proposed_lean: str,
    reasoning: str,
    market_implied: Optional[float] = None,
    proposed_confidence: str = "",
    evidence_notes: Optional[dict] = None,
) -> dict:
    """Adversarially review a PROPOSED position before commit (the local cross-family
    gate). Returns ``{"verdict": "confirm"|"revise"|"veto", "challenged_probability":
    float|None, "edge_is_real": bool, "overconfident": bool, "concerns": [str],
    "rationale": str}``. Raises LocalLLMError so the orchestrator can fall back."""
    import json as _json
    gap = None if market_implied is None else round(abs(proposed_probability - market_implied), 3)
    ev_block = "" if evidence_notes is None else f"\nEVIDENCE THE FORECASTER USED:\n{_json.dumps(evidence_notes, indent=1)}\n"
    user = (
        f"QUESTION: {question}\n"
        f"PROPOSED PROBABILITY (YES): {proposed_probability}\n"
        f"PROPOSED POSITION: {proposed_lean}   CONFIDENCE: {proposed_confidence or 'n/a'}\n"
        f"MARKET-IMPLIED (YES): {'n/a' if market_implied is None else market_implied}"
        f"{'' if gap is None else f'   DIVERGENCE FROM MARKET: {gap}'}\n"
        f"{ev_block}\n"
        f"FORECASTER REASONING:\n{reasoning}\n\n"
        'OUTPUT JSON: {"verdict": "confirm"|"revise"|"veto", '
        '"challenged_probability": <0-1 float or null>, "edge_is_real": bool, '
        '"overconfident": bool, "concerns": [str], "rationale": str}\n/no_think'
    )
    out = complete_json(_CHALLENGE_SYSTEM, user, max_tokens=2048)
    v = str(out.get("verdict", "")).lower()
    if v not in ("confirm", "revise", "veto"):
        # An unparseable verdict is itself a reason not to trust the position -> revise.
        out["verdict"] = "revise"
    out.setdefault("concerns", [])
    cp = out.get("challenged_probability")
    if isinstance(cp, (int, float)) and 0.0 <= float(cp) <= 1.0:
        out["challenged_probability"] = float(cp)
    else:
        out["challenged_probability"] = None
    return out


# ---------------------------------------------------------------------------
# Autonomous SKILL revision — fold recurring lessons into the method, no human gate.
# PROJECT DIRECTIVE (2026-06-29): the SKILL self-revises as often as necessary. Qwen
# drafts a bounded "Learned heuristics (auto-maintained)" block from the resolved
# track record; the orchestrator (plumbing) writes it into SKILL.md and commits.
# ---------------------------------------------------------------------------

_SKILL_REVISE_SYSTEM = (
    "You maintain the 'Learned heuristics (auto-maintained)' section of a superforecaster's "
    "method file. You are given the CURRENT auto-section and the system's recent resolved-market "
    "lessons (each with a pattern tag, what went right/wrong, and an actionable takeaway). "
    "Rewrite the auto-section so it captures the DURABLE, GENERALIZABLE heuristics the record "
    "supports — favor patterns that recur, drop one-off noise, keep each heuristic to one "
    "imperative sentence. This guidance must not contradict core anti-anchoring / risk-gate "
    "discipline; it refines, never overrides. Be concise: at most 12 bullet heuristics."
)


def revise_skill(current_section: str, lessons: list[dict], *, max_tokens: int = 2000) -> dict:
    """Qwen drafts the updated auto-maintained heuristics block from resolved lessons.

    Returns ``{"heuristics": [str, ...], "changed": bool, "rationale": str}``. Raises
    LocalLLMError on transport/parse failure so the caller can skip the revision this run."""
    import json as _json
    lesson_blob = _json.dumps(lessons[-40:], indent=1)  # bound the context to recent lessons
    user = (
        f"CURRENT AUTO-SECTION:\n{current_section or '(empty)'}\n\n"
        f"RESOLVED-MARKET LESSONS (most recent last):\n{lesson_blob}\n\n"
        'OUTPUT JSON: {"heuristics": [str, ...], "changed": bool, '
        '"rationale": "one line on what you changed and why"}\n/no_think'
    )
    out = complete_json(_SKILL_REVISE_SYSTEM, user, max_tokens=max_tokens)
    heur = out.get("heuristics")
    if not isinstance(heur, list):
        raise LocalLLMError(f"revise_skill returned no heuristics list: {out!r}")
    out["heuristics"] = [str(h).strip() for h in heur if str(h).strip()][:12]
    out.setdefault("changed", True)
    out.setdefault("rationale", "")
    return out


# ---------------------------------------------------------------------------
# Inline self-test (no live server required — exercises parsing + fallback)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    errors: list[str] = []

    def check(name, cond):
        if not cond:
            errors.append(name)

    # ping never raises even when the endpoint is unreachable
    try:
        _ = ping(timeout=0.2)
        check("ping_no_raise", True)
    except Exception:
        check("ping_no_raise", False)

    # JSON extraction tolerates fences + prose
    check("json_plain", _extract_json_block('{"a": 1}') == {"a": 1})
    check("json_fenced",
          _extract_json_block('```json\n{"a": 2, "b": [1,2]}\n```') == {"a": 2, "b": [1, 2]})
    check("json_prose",
          _extract_json_block('Sure, here:\n{"ok": true}\nThanks!') == {"ok": True})
    try:
        _extract_json_block("no json here")
        check("json_raises_on_garbage", False)
    except LocalLLMError:
        check("json_raises_on_garbage", True)

    # extract_evidence / critique resolve chat() from this module's globals; stub it
    # there to test the wiring + JSON handling without a live server. (Under
    # `python3 -m`, this module IS __main__, so patch globals() directly — importing
    # lib.local_llm would patch a different module object.)
    _orig_chat = chat
    globals()["chat"] = lambda *a, **k: '{"question":"Q","facts":[{"claim":"c","quote":"q"}]}'
    try:
        ev = extract_evidence("Will X happen?", "raw results", as_of="2026-06-13")
        check("evidence_parsed", bool(ev.get("facts")) and ev["facts"][0]["claim"] == "c")
        check("evidence_defaults", ev["as_of"] == "2026-06-13")
    finally:
        globals()["chat"] = _orig_chat

    globals()["chat"] = lambda *a, **k: ('{"rubric_scores":{"base_rate_established":'
                                         '{"pass":true,"reason":"ok"}},"summary":"s","biggest_miss":"m"}')
    try:
        v = critique("Q", 0.7, "reasoning", outcome=1, market_implied=0.6)
        check("critique_parsed",
              v["rubric_scores"]["base_rate_established"]["pass"] is True)
    finally:
        globals()["chat"] = _orig_chat

    # forecast(): valid probability parses; out-of-range / non-numeric raises
    globals()["chat"] = lambda *a, **k: '{"my_probability": 0.37, "my_confidence": "medium", "rationale_summary": "r"}'
    try:
        fc = forecast("Q", {"facts": []}, as_of="2026-06-13")
        check("forecast_parsed", fc["my_probability"] == 0.37 and fc["my_confidence"] == "medium")
    finally:
        globals()["chat"] = _orig_chat
    globals()["chat"] = lambda *a, **k: '{"my_probability": 1.7}'
    try:
        forecast("Q", {})
        check("forecast_rejects_bad_prob", False)
    except LocalLLMError:
        check("forecast_rejects_bad_prob", True)
    finally:
        globals()["chat"] = _orig_chat

    if errors:
        print("LOCAL_LLM TEST FAILURES:", ", ".join(errors))
        raise SystemExit(1)
    print("local_llm OK")
