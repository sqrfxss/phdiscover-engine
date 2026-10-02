"""
Guard the workflow files against the edits that silently disable them.

Found by running one: adding a step to ci.yml went through a YAML round-trip,
and PyYAML wrote `on:` back as `true:` because YAML 1.1 reads a bare `on` as a
boolean. GitHub accepted the file, ran it once, and then treated it as having no
triggers at all — the workflow appeared in the Actions list under its filename
instead of its name and never ran again.

The check is deliberately narrow: it asserts the specific shape a workflow
needs, not that the file is well-formed YAML, because "parses fine" is exactly
what was true while the file was broken.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:52}{detail[:30]}")


def triggers_of(doc: dict) -> dict:
    """
    Read the `on:` block whichever way it survived being written.

    PyYAML turns a bare `on` into the boolean True on load, so a file that was
    round-tripped has `True` as the key instead of the string "on". Both spellings
    are checked so the failure message can say which one is present.
    """
    for key in ("on", True, "true"):
        if key in doc:
            return doc[key] or {}
    return {}


def main() -> int:
    files = sorted(WORKFLOWS.glob("*.yml"))
    check("workflow files are present", bool(files), f"{len(files)} files")

    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()

        # The regression this file exists for: `true:` where `on:` belongs.
        check(f"{rel}: not written as `true:`",
              not re.search(r"^true:", text, re.M))

        doc = yaml.safe_load(text)
        check(f"{rel}: parses", isinstance(doc, dict))

        tr = triggers_of(doc)
        check(f"{rel}: has triggers", bool(tr), str(list(tr)[:3]) if tr else "none")

        if "schedule" in tr:
            # A schedule with no cron never fires, and GitHub reports the
            # workflow as valid.
            sched = tr["schedule"]
            ok = bool(sched) and all(
                isinstance(s, dict) and s.get("cron", "").strip()
                for s in sched)
            check(f"{rel}: schedule has a cron", ok)
            if ok:
                # Off-the-hour: GitHub queues every scheduled workflow on the
                # hour, so :00 is the most contended minute there is.
                crons = [s["cron"] for s in sched if isinstance(s, dict)]
                check(f"{rel}: cron avoids :00",
                      all(not c.split()[1:2] or not c.split()[1].startswith("0 ")
                          or len(c.split()[1]) > 2 for c in crons),
                      ", ".join(crons)[:40])

        jobs = doc.get("jobs") or {}
        check(f"{rel}: has jobs", bool(jobs), f"{len(jobs)} jobs")

        # Concurrency: two crawls writing one dataset silently lose one run's
        # postings, which is the failure the lock on the pipeline prevents
        # locally and this prevents here.
        if "crawl" in path.name:
            conc = doc.get("concurrency") or {}
            check(f"{rel}: concurrency declared", bool(conc),
                  str(conc.get("group"))[:34])

        # A push that interpolates an event field straight into `run:` is the
        # documented workflow-injection vector. Scanned per step, not once over
        # the whole file — the earlier version tested the file text inside the
        # step loop and so reported the same result once per step.
        UNTRUSTED = re.compile(
            r"\$\{\{\s*github\.event\.(issue|pull_request|comment|review|"
            r"head_commit|commits|client_payload|discussion|wiki)")
        for job_name, job in jobs.items():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                hits = UNTRUSTED.findall(run)
                check(f"{rel}: {job_name}/{step.get('name', step.get('uses', '?'))[:24]}"
                      f" has no untrusted interpolation",
                      not hits, str(hits[:2]) if hits else "")

    passed = sum(results)
    print(f"\n{'=' * 62}\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())