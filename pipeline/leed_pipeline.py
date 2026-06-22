#!/usr/bin/env python3
"""
LEED v5 Certification Tracker — nightly analytics pipeline
Good & Fast Industry (GNF) · Sustainability Cell

Reads the live progress log (from the Apps Script GET endpoint, or a local CSV
fallback), recomputes weighted readiness, threshold gaps and impact-area splits
for each division, and writes data/leed_state.json back into the repo. GitHub
Pages then serves that file and the dashboard hydrates from it on load.

Env:
  LEED_API   Apps Script Web-App /exec URL (optional; falls back to data/log.csv)

Run locally:  python pipeline/leed_pipeline.py
In CI:        see .github/workflows/leed-pipeline.yml
"""
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "leed_state.json"
CSV_FALLBACK = ROOT / "data" / "log.csv"

# LEED v5 BD+C category caps (≈110 total) and impact-area mapping
CAPS = {"IP": 9, "LT": 15, "SS": 10, "WE": 11, "EA": 33,
        "MR": 13, "EQ": 16, "IN": 6, "RP": 4}
IMPACT = {"EA": "decarb", "MR": "decarb", "LT": "decarb",
          "WE": "ecology", "SS": "ecology", "RP": "ecology",
          "EQ": "quality", "IP": "quality", "IN": "quality"}
THRESHOLDS = [("Certified", 40), ("Silver", 50), ("Gold", 60), ("Platinum", 80)]
TARGETS = {"Yarn Dyeing": 80, "Printing": 60, "Accessories": 60}


def fetch_rows():
    """Return list of dicts: division, credit, status, points (latest per credit)."""
    api = os.environ.get("LEED_API", "").strip()
    rows = []
    if api:
        try:
            with urllib.request.urlopen(api, timeout=30) as r:
                data = json.loads(r.read().decode())
            # GET endpoint already aggregates; convert back to flat rows
            for div, buckets in data.get("divisions", {}).items():
                for bucket, label in (("sec", "Secured"), ("prog", "In progress")):
                    for tag, pts in buckets.get(bucket, {}).items():
                        rows.append({"division": div, "credit": tag,
                                     "status": label, "points": pts})
            print(f"[leed] fetched {len(rows)} aggregated entries from API")
            return rows
        except Exception as e:  # noqa
            print(f"[leed] API fetch failed ({e}); using CSV fallback", file=sys.stderr)
    if CSV_FALLBACK.exists():
        with open(CSV_FALLBACK, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append({"division": r["Division"], "credit": r["Credit"],
                             "status": r["Status"], "points": float(r["Points"] or 0)})
        print(f"[leed] loaded {len(rows)} rows from {CSV_FALLBACK.name}")
    else:
        print("[leed] no API and no CSV — emitting empty state", file=sys.stderr)
    return rows


def tag_of(credit: str) -> str:
    return (credit[:2] or "EA").upper()


def aggregate(rows):
    divisions = {}
    for r in rows:
        div = r["division"]
        tag = tag_of(r["credit"])
        if tag not in CAPS:
            continue
        d = divisions.setdefault(div, {"sec": {}, "prog": {}})
        bucket = {"Secured": "sec", "In progress": "prog"}.get(r["status"])
        if bucket:
            d[bucket][tag] = d[bucket].get(tag, 0) + float(r["points"])
    return divisions


def diagnostics(divisions):
    """Add per-division readiness, projected level, gap-to-next-threshold, impact split."""
    out = {}
    for div, d in divisions.items():
        secured = sum(d["sec"].values())
        prog = sum(d["prog"].values())
        target = TARGETS.get(div, 60)
        # current achieved level
        level = "Not yet"
        for name, pts in THRESHOLDS:
            if secured >= pts:
                level = name
        # gap to target
        gap = max(0, target - secured)
        gap_incl_prog = max(0, target - (secured + prog))
        # impact split on secured points
        imp = {"decarb": 0, "quality": 0, "ecology": 0}
        for tag, v in d["sec"].items():
            imp[IMPACT.get(tag, "quality")] += v
        out[div] = {
            "sec": {k: round(v) for k, v in d["sec"].items()},
            "prog": {k: round(v) for k, v in d["prog"].items()},
            "secured_total": round(secured),
            "forecast_total": round(secured + prog),
            "target": target,
            "achieved_level": level,
            "readiness_pct": round(min(100, secured / target * 100)),
            "gap_to_target": round(gap),
            "gap_after_inflight": round(gap_incl_prog),
            "impact": imp,
        }
    return out


# ---- Department accountability: task → department rollup ------------------
# Mirrors the dashboard's department board. Each department owns a set of LEED
# tasks tagged "ach" (achieve, one-time) or "mnt" (maintain, recurring). A
# finished task counts full, an in-progress task counts half. Override the
# defaults any time by dropping a data/tasks.csv with columns:
#   Department,Credit,Task,Status,Phase   (Status: Done|In progress|Open|Blocked)
TASK_CSV = ROOT / "data" / "tasks.csv"
TASK_WEIGHT = {"Done": 1.0, "In progress": 0.5, "Open": 0.0, "Blocked": 0.0}

DEPT_DEFAULT = {
    "Engineering & Utilities": ["In progress", "In progress", "Open", "In progress",
                                 "Done", "Done", "In progress", "Open"],
    "EHS & Compliance": ["Done", "Done", "Done", "In progress", "In progress", "Open", "Open"],
    "Civil & Projects": ["In progress", "Open", "In progress", "Done", "Open", "Open"],
    "Procurement & Supply Chain": ["In progress", "Blocked", "Open", "Open"],
    "HR / SHRM & Admin": ["Done", "In progress", "Open", "Done", "Open"],
    "Sustainability Cell / PMO": ["In progress", "Done", "In progress", "Done",
                                   "Open", "In progress", "In progress", "Open"],
    "Finance": ["Done", "In progress", "In progress", "Open"],
    "IT & Data": ["In progress", "In progress", "Open"],
}


def dept_rollup():
    """Return {department: {complete_pct, tasks, done, in_progress, open}}.

    Source priority: live Tasks API (LEED_API) -> data/tasks.csv -> built-in defaults.
    """
    api = os.environ.get("LEED_API", "").strip()
    statuses = None
    if api:
        try:
            url = api + ("&" if "?" in api else "?") + "type=tasks"
            with urllib.request.urlopen(url, timeout=30) as r:
                tasks = json.loads(r.read().decode()).get("tasks", [])
            if tasks:
                statuses = {}
                for t in tasks:
                    statuses.setdefault(t.get("dept", "?"), []).append(t.get("status", "Open"))
                print(f"[leed] loaded {len(tasks)} live tasks from API")
        except Exception as e:  # noqa
            print(f"[leed] task API fetch failed ({e}); using CSV/defaults", file=sys.stderr)
    if statuses is None and TASK_CSV.exists():
        statuses = {}
        with open(TASK_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                statuses.setdefault(r["Department"], []).append(r["Status"])
        print(f"[leed] loaded department tasks from {TASK_CSV.name}")
    if statuses is None:
        statuses = {d: list(s) for d, s in DEPT_DEFAULT.items()}
    out = {}
    for dept, sts in statuses.items():
        n = len(sts) or 1
        score = sum(TASK_WEIGHT.get(s, 0) for s in sts)
        out[dept] = {
            "complete_pct": round(score / n * 100),
            "tasks": n,
            "done": sts.count("Done"),
            "in_progress": sts.count("In progress"),
            "open": sts.count("Open") + sts.count("Blocked"),
        }
    return out


def main():
    rows = fetch_rows()
    divisions = aggregate(rows)
    enriched = diagnostics(divisions) if divisions else {}
    departments = dept_rollup()
    portfolio = (
        round(sum(d["readiness_pct"] for d in enriched.values()) / len(enriched))
        if enriched else 0
    )
    dept_avg = (round(sum(d["complete_pct"] for d in departments.values()) / len(departments))
                if departments else 0)
    state = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rating_system": "LEED v5 BD+C",
        "portfolio_readiness_pct": portfolio,
        "department_avg_completion_pct": dept_avg,
        "divisions": enriched,
        "departments": departments,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"[leed] wrote {OUT.relative_to(ROOT)} — "
          f"portfolio readiness {portfolio}% · "
          f"dept completion {dept_avg}% across {len(departments)} departments")


if __name__ == "__main__":
    main()
