"""retrieval.py — Qwen's browser + Qwen-driven evidence gathering.

PROJECT DIRECTIVE (model routing, 2026-06-29): **Qwen does the web retrieval**, not the
orchestrator. Qwen has no native browser, so this module IS the browser: keyless web tools
(``web_search``/``wiki_lookup``/``web_fetch``) plus an agentic tool-calling loop
(``gather_evidence``) in which the LOCAL model decides what to search, what to read, and
condenses it all into structured, quoted ``EvidenceNotes``. The Opus orchestrator no longer
runs any WebSearch/WebFetch — its only jobs are running scripts, recording, committing, and
the post-mortem Defender/Judge roles.

Why these backends
------------------
* **Google News RSS** (``news.google.com/rss/search``) — keyless, reliable, and carries the
  publisher *source* per item, so we can count ">5 disparate sources" directly. Soft markets
  (politics/policy/culture/statements) are news-driven, so this is the workhorse.
* **Wikipedia API** (opensearch + REST summary) — keyless reference for base rates / entities.
* **web_fetch** — plain HTTP GET + HTML->text, for any primary URL the model wants to read.

Everything is stdlib-only (``urllib``) and degrades gracefully: a dead backend returns an empty
result, never raises into the loop. Search backend is env-pluggable (``SEARCH_BACKEND``) so a
keyed API (Brave/Tavily/Serper) or a self-hosted SearXNG can be dropped in later without
touching callers.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from . import config, local_llm

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Low-level HTTP (stdlib, never raises to caller)
# ---------------------------------------------------------------------------

def _http_get(url: str, *, timeout: float = 20.0, accept: str = "*/*") -> Optional[str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": accept,
                      "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


def _strip_html(raw: str) -> str:
    """Crude but dependency-free HTML -> readable text."""
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?is)<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Keyless search backends
# ---------------------------------------------------------------------------

def _google_news(query: str, max_results: int = 10) -> list[dict]:
    """Keyless news search via Google News RSS. Returns rows with a real publisher
    ``source`` (lets us count disparate sources) and a snippet from the item body."""
    url = ("https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}))
    body = _http_get(url, accept="application/rss+xml, application/xml;q=0.9")
    if not body:
        return []
    rows: list[dict] = []
    for item in re.findall(r"<item>(.*?)</item>", body, re.S)[:max_results]:
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.S)
            val = m.group(1) if m else ""
            val = re.sub(r"^<!\[CDATA\[|\]\]>$", "", val.strip())
            return html.unescape(val).strip()

        title = _strip_html(grab("title"))
        link = grab("link")
        source = _strip_html(grab("source")) or "news"
        date = grab("pubDate")
        snippet = _strip_html(grab("description"))[:400]
        if title:
            rows.append({"title": title, "url": link, "source": source,
                         "date": date, "snippet": snippet})
    return rows


def _searxng(query: str, max_results: int = 10) -> list[dict]:
    """Optional self-hosted SearXNG JSON backend (env SEARXNG_URL). Keyless, general web."""
    base = config.SEARXNG_URL
    if not base:
        return []
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "safesearch": "0"})
    body = _http_get(url, accept="application/json")
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    rows = []
    for r in (data.get("results") or [])[:max_results]:
        rows.append({
            "title": r.get("title", ""), "url": r.get("url", ""),
            "source": urllib.parse.urlparse(r.get("url", "")).netloc or "web",
            "date": r.get("publishedDate", "") or "",
            "snippet": (r.get("content") or "")[:400],
        })
    return [r for r in rows if r["title"]]


def web_search(query: str, max_results: int = 10) -> list[dict]:
    """General web/news search (keyless). Backend chosen by ``config.SEARCH_BACKEND``:
    ``searxng`` if configured, else Google News RSS. Returns rows
    ``{title, url, source, date, snippet}``. Never raises."""
    if config.SEARCH_BACKEND == "searxng" or config.SEARXNG_URL:
        rows = _searxng(query, max_results)
        if rows:
            return rows
    return _google_news(query, max_results)


def wiki_lookup(query: str, sentences: int = 6) -> list[dict]:
    """Keyless Wikipedia lookup for base rates / entity facts. Returns up to a few
    matching pages with an extract. Never raises."""
    api = "https://en.wikipedia.org/w/api.php?"
    body = _http_get(api + urllib.parse.urlencode(
        {"action": "opensearch", "search": query, "limit": "3", "format": "json"}))
    if not body:
        return []
    try:
        titles = json.loads(body)[1]
    except (json.JSONDecodeError, IndexError, TypeError):
        return []
    rows: list[dict] = []
    for title in titles[:3]:
        summ = _http_get("https://en.wikipedia.org/api/rest_v1/page/summary/"
                         + urllib.parse.quote(title.replace(" ", "_")))
        if not summ:
            continue
        try:
            d = json.loads(summ)
        except json.JSONDecodeError:
            continue
        extract = d.get("extract", "")
        if extract:
            rows.append({
                "title": title, "url": (d.get("content_urls", {})
                                        .get("desktop", {}).get("page", "")),
                "source": "Wikipedia", "date": d.get("timestamp", ""),
                "snippet": extract[:1200],
            })
    return rows


def web_fetch(url: str, max_chars: int = 6000) -> str:
    """Fetch a URL and return readable text (truncated). Empty string on failure."""
    if not url or not url.startswith("http"):
        return ""
    body = _http_get(url, accept="text/html,application/xhtml+xml")
    if not body:
        return ""
    return _strip_html(body)[:max_chars]


# ---------------------------------------------------------------------------
# Qwen-driven agentic retrieval loop
# ---------------------------------------------------------------------------

_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web/news for current information. Returns titles, "
                       "publisher sources, dates, and snippets. Use varied queries to hit "
                       "different angles and DIFFERENT sources.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "search query"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "wiki_lookup",
        "description": "Look up an entity/topic on Wikipedia for base rates and background.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Fetch the readable text of a specific URL returned by a prior search.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
]

_GATHER_SYSTEM = (
    "You are an autonomous research agent for a superforecaster. Given a forecasting question, "
    "you must gather decision-relevant evidence by CALLING THE TOOLS yourself — you have a web "
    "browser via web_search, wiki_lookup, and web_fetch. Method:\n"
    "1. Run SEVERAL web_search calls with different angles/keywords to reach a BROAD set of "
    "DISPARATE sources (distinct news organizations). The forecaster needs > 5 distinct sources.\n"
    "2. Use wiki_lookup for base rates / background where relevant.\n"
    "3. web_fetch a few of the most informative URLs to read past the snippet.\n"
    "4. When you have enough, STOP calling tools and output the final EvidenceNotes JSON.\n"
    "Do NOT forecast and do NOT state a probability. Only condense facts, each with a SHORT "
    "verbatim quote and its source, plus base rates and key uncertainties."
)

_GATHER_SCHEMA_HINT = (
    'FINAL OUTPUT (only after you finish searching) — ONE JSON object: '
    '{"question": str, "as_of": str, '
    '"facts": [{"claim": str, "quote": str, "source": str, "date": str, '
    '"supports": "yes"|"no"|"unclear"}], '
    '"base_rates": [str], "key_uncertainties": [str], "sources_consulted": [str]}'
)


def _run_tool(name: str, arguments: dict) -> str:
    """Dispatch a model tool-call to the real backend; return a compact text result."""
    try:
        if name == "web_search":
            rows = web_search(str(arguments.get("query", "")))
            if not rows:
                return "No results."
            return "\n".join(
                f"[{i+1}] {r['title']} — SOURCE: {r['source']} ({r['date']})\n    {r['snippet']}\n    URL: {r['url']}"
                for i, r in enumerate(rows))
        if name == "wiki_lookup":
            rows = wiki_lookup(str(arguments.get("query", "")))
            if not rows:
                return "No Wikipedia match."
            return "\n".join(f"{r['title']} (Wikipedia): {r['snippet']}" for r in rows)
        if name == "web_fetch":
            txt = web_fetch(str(arguments.get("url", "")))
            return txt or "Could not fetch that URL."
    except Exception as e:  # never let a tool crash the loop
        return f"tool error: {e}"
    return f"unknown tool {name}"


def _distinct_sources(transcript_sources: list[str]) -> int:
    norm = {s.strip().lower() for s in transcript_sources if s and s.strip()}
    return len(norm)


def gather_evidence(question: str, *, as_of: str = "", min_sources: int = 5,
                    max_steps: int = 8, model: Optional[str] = None) -> dict:
    """Qwen-driven retrieval: the LOCAL model searches/fetches the web itself and returns
    structured EvidenceNotes. Returns the notes dict (same shape ``forecast_ensemble``
    expects) annotated with ``n_sources``. Raises ``local_llm.LocalLLMError`` only if the
    model is unreachable or never produces parseable notes (caller may fall back to
    ``local_llm.extract_evidence`` over orchestrator-supplied raw text)."""
    messages = [
        {"role": "system", "content": _GATHER_SYSTEM + "\n\n" + _GATHER_SCHEMA_HINT},
        {"role": "user", "content": f"QUESTION: {question}\nAS_OF: {as_of}\n\n"
                                     "Begin gathering evidence now by calling the tools."},
    ]
    seen_sources: list[str] = []
    final_text = ""
    for step in range(max_steps):
        force_finish = step == max_steps - 1 or (
            step >= 2 and _distinct_sources(seen_sources) >= min_sources)
        msg = local_llm.chat_tools(
            messages, tools=(None if force_finish else _TOOLS),
            temperature=0.2, max_tokens=4000)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls or force_finish:
            final_text = msg.get("content") or ""
            if final_text.strip():
                break
            # model went quiet without finishing — nudge it to emit the notes
            messages.append({"role": "user", "content":
                             "Stop searching. Output the final EvidenceNotes JSON now."})
            continue
        # record the assistant turn (with its tool calls) then answer each call
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                arguments = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = _run_tool(name, arguments)
            # harvest source names from search results for the disparity count
            for line in result.splitlines():
                m = re.search(r"SOURCE:\s*([^()\n]+)", line)
                if m:
                    seen_sources.append(m.group(1))
                elif "(Wikipedia)" in line:
                    seen_sources.append("Wikipedia")
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "name": name, "content": result[:6000]})

    if not final_text.strip():
        raise local_llm.LocalLLMError("gather_evidence: model produced no final notes")
    notes = local_llm._extract_json_block(final_text)
    notes.setdefault("question", question)
    notes.setdefault("as_of", as_of)
    notes.setdefault("facts", [])
    # Authoritative source count: union of what the model listed and what we observed,
    # whitespace-normalized so "BBC" and "BBC " don't double-count.
    listed = notes.get("sources_consulted") or []
    union = {s.strip() for s in [*listed, *seen_sources] if s and s.strip()}
    notes["sources_consulted"] = sorted(union)
    notes["n_sources"] = len({s.lower() for s in union})
    return notes


# ---------------------------------------------------------------------------
# Inline self-test (live network; skips gracefully if offline)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rows = web_search("UK prime minister", max_results=5)
    print(f"web_search -> {len(rows)} rows; distinct sources:",
          _distinct_sources([r["source"] for r in rows]))
    if rows:
        print("  sample:", rows[0]["title"], "—", rows[0]["source"])
    wk = wiki_lookup("Prime Minister of the United Kingdom")
    print(f"wiki_lookup -> {len(wk)} pages")
    print("retrieval OK")
