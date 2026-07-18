#!/usr/bin/env python3
"""Recovery Card - thread intelligence.

The unit of this app is a THREAD of attention: declared work, an emergent
errand, an email someone started answering. This module owns the thread
graph and every cheap, continuous judgement about it:

  - graph.json:  threads, nodes, edges, plus provisionally-untagged frames
  - classification (tiered, cheapest first):
      1. explicit hints (park notes, corrections)  - always win
      2. metadata anchors (app, title, url, file)  - direct match settles it
      3. embedding similarity vs thread centroids  - clear winner takes it
      4. ambiguous -> provisionally untagged, healed later in hindsight
  - momentum affinity: the glance-vs-thread rule. Threads gain affinity only
    from sustained engagement across frames; a glance moves nothing.
  - retrieval: a thread's own history ranked by similarity, for card prompts
  - retrospective healing: untagged frames folded in once hindsight exists

Embeddings come from `embeddinggemma` via local Ollama (768-dim, verified).
The 12B model is never called here; this layer must stay cheap enough to run
continuously. Everything is local.
"""

import json
import math
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GRAPH = ROOT / "graph.json"

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embeddinggemma")

# --- Tunables -------------------------------------------------------------
# Thresholds calibrated against embeddinggemma's actual range, measured on
# real data: a related-but-new frame ("Genius Bar appointment" vs a speaker-
# repair thread) scores ~0.55, the nearest wrong thread ~0.41, and an
# unrelated control ("tax filing") ~0.20. 0.52 with a 0.06 margin accepts
# genuine matches and keeps everything else out.
SIM_THRESHOLD = float(os.environ.get("SIM_THRESHOLD", 0.52))   # tier-3 winner
SIM_MARGIN = float(os.environ.get("SIM_MARGIN", 0.06))         # beat runner-up by
AFFINITY_GAIN = 1.0          # per engaged, matching frame
AFFINITY_DECAY = 0.72        # everyone else, per frame
SWITCH_MOMENTUM = 3.0        # affinity needed to become the active thread
EMERGENT_MIN_FRAMES = 3      # mutually-similar untagged frames to propose
EMERGENT_SIM = 0.55          # how similar those frames must be to each other
CENTROID_WINDOW = 12         # recent embeddings kept per thread
UNTAGGED_KEEP = 40           # provisional frames retained for healing


# --- Embeddings -----------------------------------------------------------

def embed(texts):
    """Embed a list of strings locally. Returns list of vectors (or Nones)."""
    if isinstance(texts, str):
        texts = [texts]
    texts = [t[:2000] for t in texts]
    try:
        req = urllib.request.Request(
            f"{OLLAMA}/api/embed",
            data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("embeddings") or [None] * len(texts)
    except Exception:
        return [None] * len(texts)


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def mean_vec(vecs):
    vecs = [v for v in vecs if v]
    if not vecs:
        return None
    n = len(vecs)
    return [sum(v[i] for v in vecs) / n for i in range(len(vecs[0]))]


# --- Graph store ----------------------------------------------------------

def _now():
    return datetime.now().isoformat(timespec="seconds")


def load():
    if GRAPH.exists():
        try:
            g = json.loads(GRAPH.read_text())
            g.setdefault("threads", {})
            g.setdefault("nodes", [])
            g.setdefault("edges", [])
            g.setdefault("untagged", [])
            g.setdefault("affinity", {})
            g.setdefault("meta", {})
            return g
        except Exception:
            pass
    return {"threads": {}, "nodes": [], "edges": [], "untagged": [],
            "affinity": {}, "meta": {}}


def save(g):
    tmp = GRAPH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(g, indent=1))
    tmp.replace(GRAPH)


def new_thread(g, name, origin="declared", anchors=None, return_point=""):
    """Create a thread. Threads are held, never discarded."""
    tid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "thread"
    base, n = tid, 2
    while tid in g["threads"]:
        tid = f"{base}-{n}"; n += 1
    vec = embed(name)[0]
    g["threads"][tid] = {
        "id": tid, "name": name, "status": "parked", "origin": origin,
        "return_point": return_point, "last_seen": _now(), "salience": 1.0,
        "anchors": sorted(set(a.lower() for a in (anchors or []) if a)),
        "recent_embeddings": [vec] if vec else [],
        "centroid": vec,
        "history": [],  # {kind: card|park|correction|return_point, text, at}
    }
    return g["threads"][tid]


def touch(g, tid, return_point=None):
    t = g["threads"].get(tid)
    if not t:
        return
    t["last_seen"] = _now()
    if return_point:
        t["return_point"] = return_point
        t["history"].append({"kind": "return_point", "text": return_point,
                             "at": _now()})


def add_history(g, tid, kind, text):
    t = g["threads"].get(tid)
    if t and text:
        t["history"].append({"kind": kind, "text": text[:500], "at": _now()})
        t["history"] = t["history"][-40:]


def add_anchor(g, tid, anchor):
    t = g["threads"].get(tid)
    if t and anchor:
        a = anchor.lower().strip()
        if a and a not in t["anchors"]:
            t["anchors"].append(a)


def feed_embedding(g, tid, vec):
    """Fold one frame's embedding into a thread's running centroid."""
    t = g["threads"].get(tid)
    if not t or not vec:
        return
    t["recent_embeddings"].append(vec)
    t["recent_embeddings"] = t["recent_embeddings"][-CENTROID_WINDOW:]
    t["centroid"] = mean_vec(t["recent_embeddings"])


# --- Frame text context ---------------------------------------------------

def frame_text(meta):
    """One string describing a frame, for embedding and anchor matching."""
    parts = []
    if meta.get("app"):
        parts.append(meta["app"])
    parts += meta.get("window_titles", [])[:4]
    ax = meta.get("ax") or {}
    for k in ("url", "tab", "focused", "selected"):
        if ax.get(k):
            parts.append(str(ax[k]))
    if meta.get("clipboard"):
        parts.append(meta["clipboard"])
    parts += meta.get("recent_files", [])[:5]
    return " · ".join(str(p) for p in parts if p)[:2000]


def engaged(meta):
    """Was the user actually doing something in this frame?

    Typing or clicking recently counts. Pure mouse presence does not - that
    is the difference between a thread forming and a glance.
    """
    e = meta.get("engagement") or {}
    return (e.get("keys_ago", 999) < 12 or e.get("click_ago", 999) < 8
            or e.get("scroll_ago", 999) < 6)


# --- Tiered classification ------------------------------------------------

def classify(g, meta, hint_tid=None):
    """Classify one frame. Returns (tid or None, tier, score).

    None means honestly ambiguous: the frame goes to the untagged pool for
    retrospective healing. Nothing is ever forced.
    """
    # Tier 1 - explicit hints always win.
    if hint_tid and hint_tid in g["threads"]:
        return hint_tid, 1, 1.0

    text = frame_text(meta)
    low = text.lower()

    # Tier 2 - metadata anchors. A direct hit settles it.
    best_tid, best_hits = None, 0
    for tid, t in g["threads"].items():
        hits = sum(1 for a in t["anchors"] if a and a in low)
        if hits > best_hits:
            best_tid, best_hits = tid, hits
    if best_hits > 0:
        return best_tid, 2, float(best_hits)

    # Tier 3 - semantic similarity vs running centroids.
    vec = embed(text)[0] if text else None
    if vec:
        scored = sorted(
            ((cosine(vec, t.get("centroid")), tid)
             for tid, t in g["threads"].items() if t.get("centroid")),
            reverse=True)
        if scored:
            top, tid = scored[0]
            second = scored[1][0] if len(scored) > 1 else 0.0
            if top >= SIM_THRESHOLD and top - second >= SIM_MARGIN:
                return tid, 3, round(top, 3)

    # Tier 4 - ambiguous. Hold it, do not force it.
    g["untagged"].append({
        "at": _now(), "frame": meta.get("frame", ""), "text": text,
        "vec": vec, "engaged": engaged(meta),
    })
    g["untagged"] = g["untagged"][-UNTAGGED_KEEP:]
    return None, 4, 0.0


# --- Momentum affinity (the glance-vs-thread rule) ------------------------

def update_affinity(g, tid, meta):
    """One frame's worth of momentum. Returns the active tid (may change).

    Affinity builds only from engaged frames that matched a thread, and
    decays everywhere else. The active thread changes only when a
    challenger has real momentum - never on a single frame. Thrashing
    (rapid switching without engagement) builds nothing anywhere.
    """
    aff = g["affinity"]
    is_engaged = engaged(meta)

    # Decay everyone EXCEPT the thread this engaged frame matched: affinity
    # "builds with consecutive engaged frames and decays otherwise". Decaying
    # the matched thread too would make steady engagement plateau below the
    # switch threshold forever.
    reinforced = tid if (tid and is_engaged) else None
    for k in list(aff):
        if k == reinforced:
            continue
        aff[k] = round(aff[k] * AFFINITY_DECAY, 3)
        if aff[k] < 0.05:
            del aff[k]

    if reinforced:
        aff[tid] = round(aff.get(tid, 0.0) + AFFINITY_GAIN, 3)
        feed_embedding(g, tid, embed(frame_text(meta))[0])
        touch(g, tid)

    active = g["meta"].get("active_thread")
    if tid and tid != active and aff.get(tid, 0) >= SWITCH_MOMENTUM:
        if active and active in g["threads"]:
            g["threads"][active]["status"] = "parked"
        g["threads"][tid]["status"] = "active"
        g["meta"]["active_thread"] = tid
        active = tid
    return active


def emergent_candidate(g):
    """Sustained engagement that fits no known thread.

    Requires EMERGENT_MIN_FRAMES recent untagged, ENGAGED frames that are
    mutually similar - momentum, never a single frame. Returns a proposal
    dict or None. The user names and confirms it; the app never files it
    silently.
    """
    pool = [u for u in g["untagged"][-10:] if u.get("engaged") and u.get("vec")]
    if len(pool) < EMERGENT_MIN_FRAMES:
        return None
    recent = pool[-EMERGENT_MIN_FRAMES:]
    sims = [cosine(a["vec"], b["vec"])
            for i, a in enumerate(recent) for b in recent[i + 1:]]
    if sims and min(sims) >= EMERGENT_SIM:
        return {
            "frames": [u["frame"] for u in recent],
            "sample_text": recent[-1]["text"][:300],
            "coherence": round(min(sims), 3),
        }
    return None


def confirm_emergent(g, name, proposal):
    """User said yes and named it. Fold the evidence in, then heal."""
    t = new_thread(g, name, origin="emergent")
    for u in g["untagged"]:
        if u.get("frame") in set(proposal.get("frames", [])):
            feed_embedding(g, t["id"], u.get("vec"))
    heal(g)
    return t


# --- Retrospective healing ------------------------------------------------

def heal(g):
    """Re-examine provisionally-untagged frames with hindsight.

    Runs at card time and when an emergent thread is confirmed. Frames that
    now clearly match a centroid are folded in; the rest stay honestly
    untagged.
    """
    healed, keep = 0, []
    for u in g["untagged"]:
        vec = u.get("vec")
        placed = False
        if vec:
            scored = sorted(
                ((cosine(vec, t.get("centroid")), tid)
                 for tid, t in g["threads"].items() if t.get("centroid")),
                reverse=True)
            if scored and scored[0][0] >= SIM_THRESHOLD:
                feed_embedding(g, scored[0][1], vec)
                healed += 1
                placed = True
        if not placed:
            keep.append(u)
    g["untagged"] = keep
    return healed


# --- Retrieval (thread memory for card prompts) ---------------------------

def retrieve(g, tid, query, k=4):
    """The thread's own history, most relevant first."""
    t = g["threads"].get(tid)
    if not t or not t["history"]:
        return []
    qv = embed(query)[0] if query else None
    items = t["history"][-20:]
    if not qv:
        return items[-k:]
    texts = [i["text"] for i in items]
    vecs = embed(texts)
    ranked = sorted(zip(items, vecs),
                    key=lambda p: cosine(qv, p[1]), reverse=True)
    return [i for i, _ in ranked[:k]]


# --- Board data -----------------------------------------------------------

def board(g):
    """Everything the board needs, in one stable shape."""
    active_tid = g["meta"].get("active_thread")
    threads = sorted(
        g["threads"].values(),
        key=lambda t: (t["id"] != active_tid, t["last_seen"]), )
    threads = ([t for t in g["threads"].values() if t["id"] == active_tid] +
               sorted((t for t in g["threads"].values()
                       if t["id"] != active_tid),
                      key=lambda t: t["last_seen"], reverse=True))
    return {
        "active": active_tid,
        "threads": [{k: t[k] for k in
                     ("id", "name", "status", "origin", "return_point",
                      "last_seen", "salience", "anchors")}
                    for t in threads],
        "untagged_count": len(g["untagged"]),
        "affinity": g["affinity"],
    }
