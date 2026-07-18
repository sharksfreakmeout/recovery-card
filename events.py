#!/usr/bin/env python3
"""Recovery Card - resumption measurement.

An append-only local event log, so the engine room can answer the only
performance question that matters: how long from coming back to actually
working again?

Events: interruption (the moment the person left), card_ready, resume_click
(the deliberate button), engaged (first engaged frame after a return).
"""

import json
import time
from pathlib import Path

import os
ROOT = Path(__file__).resolve().parent
_BASE = Path(os.environ["RC_SANDBOX"]) if os.environ.get("RC_SANDBOX") \
    else ROOT
LOG = _BASE / "eval" / "events.jsonl"


def log_event(kind, **fields):
    try:
        LOG.parent.mkdir(exist_ok=True)
        rec = {"event": kind, "at": time.time(), **fields}
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # measurement must never break the product


def read_events(limit=400):
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def resumptions():
    """Pair each return signal with the first engaged frame after it.

    A resumption starts at resume_click when there was one (the deliberate
    path), else at card_ready (the ambient path), and ends at the next
    engaged event. Returns newest-first summaries plus the median.
    """
    evs = read_events()
    out = []
    start = None
    for e in evs:
        if e["event"] in ("resume_click", "card_ready"):
            # resume_click supersedes a card_ready for the same return
            if start is None or e["event"] == "resume_click":
                start = e
        elif e["event"] == "engaged" and start is not None:
            out.append({
                "via": start["event"],
                "thread": start.get("thread", ""),
                "seconds": round(e["at"] - start["at"], 1),
                "at": start["at"],
            })
            start = None
    out = [r for r in out if 0 <= r["seconds"] <= 3600]
    out.reverse()
    secs = sorted(r["seconds"] for r in out)
    median = secs[len(secs) // 2] if secs else None
    return {"recent": out[:12], "median_seconds": median, "count": len(out)}
