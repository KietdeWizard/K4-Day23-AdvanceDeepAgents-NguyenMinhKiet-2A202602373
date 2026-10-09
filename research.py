"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys
import time  # noqa: F401
from collections import Counter  # noqa: F401
from pathlib import Path

from agents import FINALIZER_PATH, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent  # noqa: F401
from model import make_model  # noqa: F401
from sandbox import download, open_sandbox, upload  # noqa: F401

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"   # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (topic or "").strip().lower()).strip("-")
    return (slug[:60].rstrip("-") or "topic")


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return f"""Research the topic: {topic}
Use the complete multi-agent workflow. This is a hard acceptance checklist, not a suggestion:
1) delegate at least three researchers;
2) obtain at least one exact arXiv record from arxiv_search, one exact Hugging Face record from hf_search_papers or
hf_daily_papers, and one exact web record from web_search or web_fetch;
3) copy the returned JSON records exactly into {SOURCES_PATH}, using source values arxiv, hf-search/hf-daily, and web;
4) cite those exact records in the report. Never substitute homepages, `/html/`, `/daily/`, blog URLs, Google, DOI examples,
or invented records. If any required family fails, retry it before writing the report.
The final report must be written to {REPORT_PATH}; sources to {SOURCES_PATH}."""


def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}.

    PSEUDO-CODE: walk the lead's messages; for every message with tool_calls count call["name"] (subagent_calls = the
    count of "task"); add the input/output token counts from each message's usage_metadata when present.
    (Lead messages only: subagent tokens are not included, so this undercounts the real cost.)
    elapsed_s rounded to 0.1.
    """
    tool_calls, subagent_calls = Counter(), 0
    input_tokens = output_tokens = 0
    for message in messages or []:
        calls = getattr(message, "tool_calls", None) or (message.get("tool_calls", []) if isinstance(message, dict) else [])
        for call in calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            tool_calls[name] += 1
            subagent_calls += name == "task"
        usage = getattr(message, "usage_metadata", None) or (message.get("usage_metadata", {}) if isinstance(message, dict) else {})
        input_tokens += int(usage.get("input_tokens", 0) or 0)
        output_tokens += int(usage.get("output_tokens", 0) or 0)
    return {"model": model_name, "elapsed_s": round(elapsed, 1), "subagent_calls": subagent_calls,
            "tool_calls": dict(tool_calls), "tokens": {"input": input_tokens, "output": output_tokens}}


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path.

    PSEUDO-CODE:
      files = download(backend, [REPORT_PATH, SOURCES_PATH])
      if the report is missing/empty or sources.json is missing/invalid JSON: raise RuntimeError and WRITE NOTHING
          (a failed run must never leave an empty or half-written report behind)
      write <slug>.sources.json, <slug>.meta.json (topic + summarize(...) + n_sources + source_families: the sorted
      distinct "source" values of sources.json) and <slug>.md
    """
    files = download(backend, [REPORT_PATH, SOURCES_PATH])
    report_bytes, source_bytes = files.get(REPORT_PATH), files.get(SOURCES_PATH)
    if not report_bytes or not report_bytes.strip() or not source_bytes:
        raise RuntimeError("sandbox did not produce a non-empty report and sources.json")
    try:
        report_text = report_bytes.decode("utf-8")
        sources = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"invalid sandbox output: {exc}") from exc
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("sources.json must be a non-empty JSON list")
    allowed = {"arxiv", "hf-daily", "hf-search", "web"}
    for source in sources:
        if not isinstance(source.get("n"), int):
            raise RuntimeError("every source must have an integer n")
        if source.get("source") not in allowed:
            raise RuntimeError(f"invalid source family: {source.get('source')!r}")
        url = str(source.get("url") or "")
        family = source["source"]
        if family == "arxiv" and not re.match(r"^https://arxiv\.org/abs/[A-Za-z0-9.:-]+$", url):
            raise RuntimeError(f"arxiv source has invalid URL: {url}")
        if family in {"hf-daily", "hf-search"} and not re.match(r"^https://huggingface\.co/papers/[A-Za-z0-9.:-]+$", url):
            raise RuntimeError(f"Hugging Face source has invalid URL: {url}")
        if family == "web" and (not re.match(r"^https?://", url) or "example" in url.lower() or "google.com" in url.lower()):
            raise RuntimeError(f"web source has invalid URL: {url}")
    if len({source.get("source") for source in sources}) < 3:
        raise RuntimeError("sources.json must contain at least three source families")
    if "## References" not in report_text:
        raise RuntimeError("report is missing the finalized References section")
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = slugify(topic)
    meta = {"topic": topic, **summarize(messages, elapsed, model_name), "n_sources": len(sources),
            "source_families": sorted({str(s.get("source")) for s in sources if s.get("source")})}
    outputs = {reports_dir / f"{stem}.md": report_text, reports_dir / f"{stem}.sources.json": json.dumps(sources, ensure_ascii=False, indent=2), reports_dir / f"{stem}.meta.json": json.dumps(meta, ensure_ascii=False, indent=2)}
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")
    return reports_dir / f"{stem}.md"


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic).

    PSEUDO-CODE:
      empty topic -> print usage to stderr, return 2
      model = make_model(); start = time.monotonic()
      with open_sandbox() as backend:                # the sandbox is always cleaned up, even on errors
          backend.execute("mkdir -p <WORKDIR>/research/notes <WORKDIR>/report")
          upload(backend, {VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(), FINALIZER_PATH: FINALIZER_SOURCE.read_bytes()})
          agent = build_lead_agent(backend, model)
          result = agent.invoke({"messages": [{"role": "user", "content": build_prompt(topic)}]},
                                config={"recursion_limit": 1000})
          save_outputs(...); on RuntimeError print "FAILED: ..." to stderr and return 1
      print where the report was saved; return 0
    """
    if not topic.strip():
        print("Usage: python research.py \"topic\"", file=sys.stderr)
        return 2
    try:
        model = make_model()
        model_name = os.getenv("LAB_MODEL") or os.getenv("OPENAI_DEPLOYMENT_MODEL") or type(model).__name__
        start = time.monotonic()
        with open_sandbox() as backend:
            result = backend.execute(f"mkdir -p {WORKDIR}/research/notes {WORKDIR}/report")
            if result.exit_code != 0:
                raise RuntimeError(f"could not initialize sandbox: {result.output}")
            upload(backend, {VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(), FINALIZER_PATH: FINALIZER_SOURCE.read_bytes()})
            agent = build_lead_agent(backend, model)
            result = agent.invoke({"messages": [{"role": "user", "content": build_prompt(topic)}]}, config={"recursion_limit": 1000})
            messages = result.get("messages", []) if isinstance(result, dict) else getattr(result, "messages", [])
            path = save_outputs(backend, topic, messages, time.monotonic() - start, model_name)
        print(f"Report saved to {path}")
        return 0
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
