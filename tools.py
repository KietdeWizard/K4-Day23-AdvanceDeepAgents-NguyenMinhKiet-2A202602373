"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json  # noqa: F401
import os  # noqa: F401
import random
import re
import threading
import time  # noqa: F401
import xml.etree.ElementTree  # noqa: F401  (arXiv answers with Atom XML)

import httpx  # noqa: F401
from langchain_core.tools import tool

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


_ARXIV_LOCK = threading.Lock()
_LAST_ARXIV_CALL = 0.0


def _retry_after(response):
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _request(client, method, url, **kwargs):
    try:
        response = client.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        raise RetryableError(str(exc)) from exc
    if response.status_code in {429, 500, 502, 503, 504}:
        raise RetryableError(f"HTTP {response.status_code}", _retry_after(response))
    response.raise_for_status()
    return response


def _compact(value, limit=None):
    text = " ".join(str(value or "").split())
    return text[:limit] if limit else text


def _safe_error(exc):
    key = os.getenv("EXA_API_KEY")
    message = str(exc)
    return message.replace(key, "[REDACTED]") if key else message


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError, wait and call it again.

    PSEUDO-CODE:
      for attempt in 0 .. attempts-1:
          try: return fn()
          except RetryableError as e:
              if this was the last attempt: raise
              delay = e.retry_after if the server told us, else exponential backoff base * 2**attempt
              cap the delay at `cap` seconds; add random jitter to the exponential case
              sleep(delay)
    Use it to wrap EVERY network call below. Also treat these as retryable: HTTP 429/500/502/503/504,
    httpx.TransportError (timeouts, connection resets). Read the Retry-After header when present.
    """
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            return fn()
        except RetryableError as exc:
            if attempt == attempts - 1:
                raise
            delay = exc.retry_after
            if delay is None:
                delay = min(cap, base * (2 ** attempt))
                delay += random.uniform(0, min(1.0, delay * 0.25))
            else:
                delay = min(cap, max(0.0, delay))
            time.sleep(delay)


# ---- TODO 2: arXiv ----
@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    # PSEUDO-CODE:
    #   keep only word characters of `query` -> terms; no terms -> "NO RESULTS" (do not call the network)
    #   respect arXiv etiquette: at least 3 seconds between two arXiv calls (remember the time of the last call)
    #   GET ARXIV_URL params: search_query="all:t1 AND all:t2 ...", sortBy=submittedDate, sortOrder=descending,
    #       max_results=clamp(max_results, 1, 30)           (wrap in with_retry)
    #   parse the Atom XML: each <entry> -> {id (last part of <id> after /abs/), url, published[:10], title, summary}
    #       collapse whitespace/newlines in title and summary; cut summary to ~600 chars
    #   no entries -> "NO RESULTS"; else json.dumps(records, ensure_ascii=False)
    #   any exception -> "ERROR: <type>: <message>"
    global _LAST_ARXIV_CALL
    try:
        terms = re.findall(r"[A-Za-z0-9-]+", query or "")
        if not terms:
            return "NO RESULTS"
        with _ARXIV_LOCK:
            wait = 3.0 - (time.monotonic() - _LAST_ARXIV_CALL)
            if wait > 0:
                time.sleep(wait)
            _LAST_ARXIV_CALL = time.monotonic()
        search = " AND ".join(f"all:{term}" for term in terms)
        limit = max(1, min(30, int(max_results)))
        def call():
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                return _request(client, "GET", ARXIV_URL, params={"search_query": search,
                    "sortBy": "submittedDate", "sortOrder": "descending", "max_results": limit, "start": 0})
        response = with_retry(call, attempts=7, base=2.0, cap=60.0)
        root = xml.etree.ElementTree.fromstring(response.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        records = []
        for entry in root.findall("a:entry", ns):
            raw_id = _compact(entry.findtext("a:id", "", ns)).rsplit("/", 1)[-1]
            paper_id = re.sub(r"v\d+$", "", raw_id)
            if not paper_id:
                continue
            records.append({"id": paper_id, "url": f"https://arxiv.org/abs/{paper_id}",
                "published": _compact(entry.findtext("a:published", "", ns))[:10],
                "title": _compact(entry.findtext("a:title", "", ns)),
                "summary": _compact(entry.findtext("a:summary", "", ns), 600)})
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_safe_error(exc)}"


# ---- TODO 3: Hugging Face ----
@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    # PSEUDO-CODE:
    #   GET HF_DAILY_URL params: limit (clamp 1..100) and date (only when given)      (with_retry)
    #   response = list of items {"paper": {id, title, summary, upvotes, githubRepo, githubStars, publishedAt}, ...}
    #   map every item to the record shape above (skip items without paper.id); url = https://huggingface.co/papers/<id>
    #   keyword -> keep records whose title+summary contains it (case-insensitive); sort by upvotes descending
    try:
        params = {"limit": max(1, min(100, int(limit)))}
        if date:
            params["date"] = date
        with httpx.Client(timeout=30) as client:
            response = with_retry(lambda: _request(client, "GET", HF_DAILY_URL, params=params))
        records = _hf_records(response.json(), keyword=keyword)
        records.sort(key=lambda item: item["upvotes"], reverse=True)
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_safe_error(exc)}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    # PSEUDO-CODE:
    #   GET HF_SEARCH_URL params: q=query, limit (clamp 1..50)                         (with_retry)
    #   same item shape as the daily endpoint; prefer paper["ai_summary"] over paper["summary"] when present
    try:
        if not (query or "").strip():
            return "NO RESULTS"
        with httpx.Client(timeout=30) as client:
            response = with_retry(lambda: _request(client, "GET", HF_SEARCH_URL,
                                                    params={"q": query, "limit": max(1, min(50, int(limit)))}))
        records = _hf_records(response.json(), prefer_ai=True)
        return json.dumps(records) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_safe_error(exc)}"


def _hf_records(items, keyword="", prefer_ai=False):
    records = []
    for item in items if isinstance(items, list) else []:
        paper = item.get("paper") if isinstance(item, dict) else None
        if not isinstance(paper, dict) or not paper.get("id"):
            continue
        title = paper.get("title") or item.get("title") or "Untitled"
        summary = (paper.get("ai_summary") if prefer_ai else None) or paper.get("summary") or item.get("summary") or ""
        if keyword and keyword.lower() not in f"{title} {summary}".lower():
            continue
        records.append({"id": str(paper["id"]), "url": f"https://huggingface.co/papers/{paper['id']}",
                        "published": _compact(paper.get("publishedAt") or item.get("publishedAt"))[:10],
                        "title": _compact(title), "summary": _compact(summary, 600),
                        "upvotes": int(paper.get("upvotes") or 0), "github": paper.get("githubRepo") or "",
                        "stars": int(paper.get("githubStars") or 0)})
    return records


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    # PSEUDO-CODE:
    #   call the MCP tool "web_search_exa" with arguments {query, objective, numResults}
    #       (objective is REQUIRED by Exa: when empty, build one from the query)
    #   see GUIDE.md part 1.4 for how to call an MCP server over plain HTTP (JSON-RPC "tools/call") and read the answer
    #   read optional env EXA_API_KEY; when present it is sent to the Exa endpoint.
    #       (see GUIDE.md 1.4 for where it goes) => the key then appears in exception text: redact it before returning "ERROR: ..."
    #   WATCH OUT: read GUIDE.md 1.4 about how Exa signals "rate limited" on the free tier, and retry on it
    try:
        objective = objective.strip() or f"Find reliable sources about {query}"
        return _exa_call("web_search_exa", {"query": query, "objective": objective,
                                            "numResults": max(1, min(10, int(num_results)))}, 60000)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_safe_error(exc)}"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    # PSEUDO-CODE: MCP tool "web_fetch_exa" with arguments {"urls": [url]}; truncate the text to ~12000 chars
    try:
        if not (url or "").strip():
            return "NO RESULTS"
        return _exa_call("web_fetch_exa", {"urls": [url]}, 60000)[:12000]
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_safe_error(exc)}"


def _exa_call(name, arguments, cap):
    endpoint = EXA_URL
    key = os.getenv("EXA_API_KEY")
    if key:
        endpoint = f"{endpoint}?exaApiKey={httpx.QueryParams({'k': key})['k']}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    def call():
        with httpx.Client(timeout=60) as client:
            response = _request(client, "POST", endpoint, headers={"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}, json=payload)
        data = None
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                    break
                except json.JSONDecodeError:
                    continue
        if data is None:
            data = response.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        result = data.get("result") or {}
        meta_text = json.dumps(result.get("_meta", {})).lower()
        content = "\n".join(x.get("text", "") for x in result.get("content", [])
                              if isinstance(x, dict) and x.get("type") == "text")
        if "rate" in meta_text and ("limit" in meta_text or "thrott" in meta_text):
            raise RetryableError("Exa rate limit")
        if not content:
            return "NO RESULTS"
        return content
    return with_retry(call, attempts=8, base=2.0, cap=cap)


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
