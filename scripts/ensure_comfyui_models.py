#!/usr/bin/env python3
"""Check a ComfyUI host for the model files a workflow template needs, and download what is missing.

Reads .env like the pipeline. Without --workflow it checks the configured templates
(VIDEO_COMFYUI_WORKFLOW on the video host, AVATAR_COMFYUI_WORKFLOW and HERO_COMFYUI_WORKFLOW
on the avatar host).

    .venv/bin/python scripts/ensure_comfyui_models.py                       # audit both hosts, no downloads
    .venv/bin/python scripts/ensure_comfyui_models.py --download --wait     # download what is missing and wait
    .venv/bin/python scripts/ensure_comfyui_models.py --host video --workflow comfyui-workflows/video_ltx2_3_ia2v-extend.json --download
    .venv/bin/python scripts/ensure_comfyui_models.py --check-manifest      # every template reference has a manifest row?

Exit status: 0 when every checked template is satisfied (or every download finished), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import comfyui_models  # noqa: E402


def load_env() -> None:
    """Load .env like the pipeline does, without importing the database-backed service modules."""
    try:
        import dotenv
    except ImportError:
        return
    dotenv.load_dotenv(PROJECT_ROOT / ".env", override=True)


def _human(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


def print_target(target: dict) -> None:
    print(f"== {target['host']} {target['base_url']}  {Path(target['workflow']).name}")
    if "error" in target:
        print(f"   error: {target['error']['message']}")
        return
    for model in target["models"]:
        state = {True: "present", False: "MISSING", None: "unverified"}[model["installed"]]
        src = "" if model["in_manifest"] else "  (no manifest entry)"
        print(f"   [{state:10s}] {model['folder'] or '?':22s} {model['filename']}{src}")
    if target.get("missing_classes"):
        print(f"   node classes not installed on the host: {target['missing_classes']}")
    for job in target.get("started") or []:
        print(f"   started: {job.get('filename')} ({job.get('status')}) via {target.get('downloader')}")
    for err in target.get("errors") or []:
        print(f"   error: {err.get('filename') or ''} {err['message']}")


def wait_for_jobs(host: str | None, poll_seconds: float, job_ids: set[str]) -> bool:
    """Poll the hosts until none of the given jobs is queued, downloading, or verifying. Returns True when none of them failed."""
    all_ok = True
    while True:
        report = comfyui_models.jobs_configured(host)
        active = []
        lines = []
        for entry in report["hosts"]:
            for job in entry.get("jobs") or []:
                if job.get("id") not in job_ids:
                    continue
                if job["status"] in ("queued", "downloading", "verifying"):
                    active.append(job)
                    pct = f"{job['percent']:.1f}%" if job.get("percent") is not None else "?"
                    lines.append(f"   {entry['host']}: {job['filename']} {job['status']} {pct} {_human(job['bytes_done'])}/{_human(job['bytes_total'])} {_human(job['speed_bps'])}/s")
                elif job["status"] == "failed":
                    all_ok = False
        if not active:
            return all_ok
        print("\n".join(lines))
        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", choices=("video", "avatar"), help="Only this ComfyUI host (default: both).")
    parser.add_argument("--workflow", help="One template to check instead of the configured ones.")
    parser.add_argument("--download", action="store_true", help="Start downloads on the host for missing manifest models.")
    parser.add_argument("--wait", action="store_true", help="With --download: poll until every job finishes.")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--check-manifest", action="store_true", help="Report template references without a manifest entry and exit.")
    parser.add_argument("--json", action="store_true", help="Print the raw result as JSON.")
    args = parser.parse_args()

    load_env()
    if args.check_manifest:
        report = comfyui_models.check_manifest()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"manifest: {report['manifest']}")
            print(f"covered: {len(report['covered'])} files")
            for name, templates in report["uncovered"].items():
                print(f"UNCOVERED {name}  (used by {', '.join(templates)})")
            for name in report["unused"]:
                print(f"unused manifest entry: {name}")
        return 0 if report["ok"] else 1

    result = comfyui_models.ensure_configured(args.host, args.workflow, download=args.download)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if not result["targets"]:
            print("nothing to check: no ComfyUI host and template configured for that selection")
        for target in result["targets"]:
            print_target(target)
    ok = result["ok"]
    started_ids = {j.get("id") for t in result["targets"] for j in (t.get("started") or []) if j.get("id")}
    if args.download and args.wait and started_ids:
        finished_ok = wait_for_jobs(args.host, args.poll_seconds, started_ids)
        after = comfyui_models.audit_configured(args.host, args.workflow)
        if not args.json:
            print("\nafter downloads:")
            for target in after["targets"]:
                print_target(target)
        ok = finished_ok and after["ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
