#!/usr/bin/env python3
"""Mark a Polo Pipeline handoff job complete (or failed).

Usage:
  python3 complete_handoff.py <job_id> --summary "Route ready: 8 stops"
  python3 complete_handoff.py <job_id> --failed --summary "Bot unavailable"
  python3 complete_handoff.py <job_id> --summary "Priced at $28" --result '{"price":28}'

Posts to the local bridge (default http://127.0.0.1:8765) or writes files
directly if --direct is set and the server is not required.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import os

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("POLO_DATA_DIR") or (ROOT / "var")).resolve()
PENDING = DATA_DIR / "handoffs" / "pending"
DONE = DATA_DIR / "handoffs" / "done"
DEFAULT_BASE = os.environ.get("POLO_URL") or f"http://127.0.0.1:{os.environ.get('POLO_PORT', '8765')}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_pending(job_id: str) -> tuple[dict | None, Path | None]:
    for path in PENDING.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("id") == job_id:
            return data, path
    return None, None


def complete_direct(job_id: str, status: str, summary: str, result) -> dict:
    DONE.mkdir(parents=True, exist_ok=True)
    job, pending_path = find_pending(job_id)
    if not job:
        # maybe already only known by id
        job = {"id": job_id, "action": None, "targetBot": None, "payload": {}, "createdAt": None}

    out = {
        "id": job_id,
        "createdAt": job.get("createdAt"),
        "completedAt": utc_now(),
        "action": job.get("action"),
        "targetBot": job.get("targetBot"),
        "payload": job.get("payload", {}),
        "status": status,
        "summary": summary,
        "result": result,
    }
    path = DONE / f"{job_id}.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    if pending_path and pending_path.exists():
        pending_path.unlink()
    return out


def complete_api(base: str, job_id: str, status: str, summary: str, result) -> dict:
    url = f"{base.rstrip('/')}/api/handoffs/{job_id}/complete"
    body = json.dumps({"status": status, "summary": summary, "result": result}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Complete a Polo Pipeline handoff job")
    parser.add_argument("job_id", help="Job id from pending handoff JSON")
    parser.add_argument("--summary", "-s", default="", help="Human-readable summary for the UI")
    parser.add_argument("--failed", action="store_true", help="Mark as failed instead of completed")
    parser.add_argument(
        "--result",
        "-r",
        default=None,
        help="Optional JSON string for structured result",
    )
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"Bridge server base URL (default {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Write files under handoffs/ without calling the HTTP API",
    )
    args = parser.parse_args()

    status = "failed" if args.failed else "completed"
    result = None
    if args.result is not None:
        try:
            result = json.loads(args.result)
        except json.JSONDecodeError as e:
            print(f"Invalid --result JSON: {e}", file=sys.stderr)
            return 2

    try:
        if args.direct:
            out = complete_direct(args.job_id, status, args.summary, result)
        else:
            try:
                out = complete_api(args.base, args.job_id, status, args.summary, result)
            except urllib.error.URLError as e:
                print(f"API unavailable ({e}); falling back to --direct", file=sys.stderr)
                out = complete_direct(args.job_id, status, args.summary, result)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
