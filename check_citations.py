"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import sys
import re

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK).

    PSEUDO-CODE:
      problems = []
      if sources is empty: return ["no sources in sources.json"]
      for each source entry:
          n must be an int                       -> problem if not
          url must start with http:// or https://-> problem if not
          the same url must not appear twice     -> problem if duplicated
      split report_text at the heading "## References":
          body = text before it; if the heading is missing -> problem
      cited = set of numbers found as [n] in the BODY only (not in the reference list; use a regex)
      every number in `cited` must exist in sources -> problem "[n] cited but missing from sources.json"
      every source number must be in `cited`        -> problem "source [n] never cited"
      the lines of the References section that start with "[n]" (regex) are the reference lines:
          every source needs exactly ONE reference line (none missing, no number twice, no number that is not a source)
          each reference line holds exactly ONE http(s) URL and it must equal that source's url
          (a line bundling several sources under one number is a problem)
      return problems
    """
    problems = []
    if not isinstance(sources, list) or not sources:
        return ["no sources in sources.json"]
    by_n = {}
    urls = set()
    for source in sources:
        n, url = source.get("n"), source.get("url") if isinstance(source, dict) else (None, None)
        if not isinstance(n, int) or isinstance(n, bool):
            problems.append(f"source n={n!r} is not an int")
        if not isinstance(url, str) or not re.match(r"^https?://", url):
            problems.append(f"source {n!r} has invalid url")
        elif url in urls:
            problems.append(f"duplicate source url: {url}")
        else:
            urls.add(url)
        if isinstance(n, int):
            if n in by_n: problems.append(f"duplicate source number [{n}]")
            by_n[n] = source
    match = re.search(r"(?m)^##[ \t]+References[ \t]*$", report_text)
    if not match:
        return problems + ["missing ## References heading"]
    body, refs = report_text[:match.start()], report_text[match.end():]
    code = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
    body_parts = code.split(body)
    cited = set()
    citation_re = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()");
    for index, part in enumerate(body_parts):
        if index % 2: continue
        for group in citation_re.findall(part):
            for piece in re.split(r"\s*,\s*", group):
                span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", piece)
                cited.update(range(int(span.group(1)), int(span.group(2)) + 1) if span and int(span.group(2))-int(span.group(1)) <= 200 else [int(span.group(1)) if span else int(piece)])
    for n in sorted(cited - set(by_n)): problems.append(f"[{n}] cited but missing from sources.json")
    for n in sorted(set(by_n) - cited): problems.append(f"source [{n}] never cited")
    ref_lines = [line.strip() for line in refs.splitlines() if re.match(r"^\[\d+\]", line.strip())]
    seen = set()
    for line in ref_lines:
        number = int(re.match(r"^\[(\d+)\]", line).group(1))
        if number in seen: problems.append(f"reference [{number}] appears more than once")
        seen.add(number)
        if number not in by_n: problems.append(f"reference [{number}] is not in sources.json")
        found_urls = re.findall(r"https?://\S+", line)
        if len(found_urls) != 1: problems.append(f"reference [{number}] must contain exactly one URL")
        elif number in by_n and found_urls[0].rstrip(".,)") != by_n[number].get("url"):
            problems.append(f"reference [{number}] URL does not match sources.json")
    if seen != set(by_n):
        problems.append("References must contain exactly one line for every source")
    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
