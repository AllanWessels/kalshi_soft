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
    payload = {
        "model": model or config.LOCAL_LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    resp = _post("chat/completions", payload, timeout=timeout)
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LocalLLMError(f"unexpected chat response shape: {resp!r}") from e


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


def complete_json(system: str, user: str, *, max_tokens: int = 1024) -> dict:
    """Chat with a JSON-only instruction and return the parsed object."""
    messages = [
        {"role": "system", "content": system +
         "\n\nRespond with ONE valid JSON object and nothing else. No prose, no markdown."},
        {"role": "user", "content": user},
    ]
    return _extract_json_block(chat(messages, max_tokens=max_tokens))


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
# Forecasting tier — LOCAL model as forecaster (the L* strategy arms)
# ---------------------------------------------------------------------------

_FORECASTER_SYSTEM = (
    "You are a calibrated superforecaster for human-behavior markets (politics, policy, "
    "culture). Reason from the supplied evidence notes ONLY. Follow the method: establish a "
    "base rate / reference class first, update incrementally on the evidence, run a quick "
    "pre-mortem, and avoid overreacting to a single noisy signal. Output a GRANULAR probability "
    "(not lazy round numbers). You are NOT given the market price — do not guess it. Judge "
    "epistemic confidence separately from the probability."
)


def forecast(question: str, evidence_notes: dict, *, as_of: str = "") -> dict:
    """LOCAL-model forecaster (the L* arms). Returns
    ``{"my_probability": float, "my_confidence": "low|medium|high",
    "rationale_summary": str, "key_drivers": [str], "reference_classes": [str]}``.
    Raises LocalLLMError so the orchestrator can fall back to an Opus forecaster."""
    import json as _json
    # /no_think: Qwen3 is a hybrid reasoning model — without this it spends the token
    # budget on a <think> block and can return no JSON. These structured tasks don't
    # need chain-of-thought, and disabling it is faster + cheaper.
    user = (
        f"QUESTION: {question}\n"
        f"AS_OF: {as_of}\n\n"
        f"EVIDENCE NOTES (JSON):\n{_json.dumps(evidence_notes, indent=2)}\n\n"
        'OUTPUT JSON: {"my_probability": <0-1 float>, "my_confidence": "low|medium|high", '
        '"rationale_summary": str, "key_drivers": [str], "reference_classes": [str]}\n/no_think'
    )
    # Local model is free + unmetered — cap is just truncation headroom, not a budget.
    out = complete_json(_FORECASTER_SYSTEM, user, max_tokens=2048)
    # Clamp + validate the probability so a malformed value can't poison the record.
    p = out.get("my_probability")
    if not isinstance(p, (int, float)) or not (0.0 <= float(p) <= 1.0):
        raise LocalLLMError(f"local forecaster returned invalid probability: {p!r}")
    out["my_probability"] = float(p)
    out.setdefault("my_confidence", "low")
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
