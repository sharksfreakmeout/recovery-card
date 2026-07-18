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
_BASE = Path(os.environ["RC_SANDBOX"]) if os.environ.get("RC_SANDBOX") \
    else ROOT
GRAPH = _BASE / "graph.json"

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
            data=json.dumps({"model": EMBED_MODEL, "input": texts,
                             # tiny (673 MB) and called continuously:
                             # stays resident, unlike the 12B
                             "keep_alive": "60m"}).encode(),
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
        # DESIGN.md voice: no engine-speak reaches the user. Return-points
        # are shown verbatim on the board and in headlines.
        rp = " ".join(return_point.replace("`", "").split())
        t["return_point"] = rp
        t["history"].append({"kind": "return_point", "text": rp,
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


_EDITOR_APPS = {"Cursor", "Code", "Visual Studio Code", "Zed", "Sublime Text",
                "Xcode", "IntelliJ IDEA", "PyCharm"}


def editor_folder_token(meta):
    """The project/folder name from an editor's window title.

    Editors title windows "file — folder" (or just "folder"). That folder
    name is the strongest anchor there is for telling two editor windows
    apart - "capture.py — Gemma Hackathon" belongs to a different thread
    than "Gate_Report.md — phossil-production-app".
    """
    if meta.get("app") not in _EDITOR_APPS:
        return ""
    title = meta.get("window_title") or ""
    for sep in (" — ", " - ", " – "):
        if sep in title:
            return title.rsplit(sep, 1)[-1].strip().lower()
    return title.strip().lower()


def freshly_engaged(meta):
    """Engagement that happened ON this frame, not carried over from the
    previous app. keys_ago<12 is right for affinity, but wrong for source
    attribution: two seconds after cmd-tabbing out of Cursor, a Claude
    frame still shows keys_ago~4 from typing IN CURSOR - and that put a
    Claude chip on a thread with no engaged Claude use. Source accrual
    demands tighter freshness than affinity does."""
    e = meta.get("engagement") or {}
    return (e.get("keys_ago", 999) < 4 or e.get("click_ago", 999) < 2.5
            or e.get("scroll_ago", 999) < 3)


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

    # Tier 1 still - sticky assignments. The user said where this belongs;
    # the classifier may never override that.
    for label, tid in (g.get("sticky") or {}).items():
        if label and label in low and tid in g["threads"] \
                and g["threads"][tid].get("status") != "archived":
            return tid, 1, 1.0

    def live(t):
        return t.get("status") != "archived"

    # Tier 2 - metadata anchors, FRONTMOST FIRST. window_titles lists every
    # window, so two Cursor projects both appear in every frame's text and
    # whichever anchor matched first used to win. The frontmost title (plus
    # the editor's folder token and the AX tab/url) is what the person is
    # actually looking at - it alone settles the match; the full text is
    # only consulted when the frontmost says nothing.
    front_parts = [meta.get("window_title") or ""]
    ax = meta.get("ax") or {}
    front_parts += [str(ax.get(k) or "") for k in ("tab", "url")]
    ftok = editor_folder_token(meta)
    if ftok:
        front_parts.append(ftok)
    front = " · ".join(p for p in front_parts if p).lower()

    # FRONTMOST ONLY - no fallback to the full text. Background window
    # titles are context for tier-3 embeddings, never anchor evidence:
    # the fallback version of this code absorbed sustained Amazon
    # shopping into a work thread because the work project's Cursor
    # window sat open in the background of every shopping frame.
    if front:
        best_tid, best_hits = None, 0
        for tid, t in g["threads"].items():
            if not live(t):
                continue
            hits = sum(1 for a in t["anchors"] if a and a in front)
            if hits > best_hits:
                best_tid, best_hits = tid, hits
        if best_hits > 0:
            return best_tid, 2, float(best_hits)

    # Tier 3 - semantic similarity vs running centroids. Embed the
    # FRONTMOST context only: embedding the full desktop meant a
    # background project window pulled shopping frames toward the work
    # thread's centroid (the second half of the Amazon absorption bug -
    # the anchor half was tier 2's background fallback). Where attention
    # IS is the frontmost window; the rest of the desktop is scenery.
    front_embed_parts = [meta.get("app") or "", front]
    if meta.get("clipboard"):
        front_embed_parts.append(meta["clipboard"])
    front_text = " · ".join(p for p in front_embed_parts if p)[:2000]
    vec = embed(front_text)[0] if front_text.strip(" ·") else None
    if vec:
        scored = sorted(
            ((cosine(vec, t.get("centroid")), tid)
             for tid, t in g["threads"].items()
             if t.get("centroid") and live(t)),
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
        # Restore data: exact URLs and the frontmost app are the two things
        # verifiably reopenable later. Collected here, spent by Resume.
        t = g["threads"][tid]
        url = (meta.get("ax") or {}).get("url")
        # Restore + source data require FRESH engagement: two seconds
        # after cmd-tabbing away from typing, the old keystrokes still
        # read as "engaged" and a passing frontmost app earns credit it
        # never had. Affinity keeps the looser window; attribution and
        # restore do not.
        if freshly_engaged(meta):
            if url:
                urls = [u for u in t.get("recent_urls", []) if u != url]
                t["recent_urls"] = (urls + [url])[-5:]
            if meta.get("app") and meta["app"] != "hidden":
                t["last_app"] = meta["app"]

            # SOURCE CHIPS accrual: only real classified frames with
            # fresh engagement. Archived threads stop classifying, so
            # chips freeze naturally.
            key = None
            if url:
                try:
                    host = url.split("//", 1)[-1].split("/", 1)[0]
                    key = host.removeprefix("www.")
                except Exception:
                    key = None
            if not key and meta.get("app") and \
                    meta["app"].lower() not in ("hidden", "unknown"):
                key = meta["app"]
            if key:
                agg = t.setdefault("sources_agg", {})
                e = agg.get(key) or {"count": 0, "first": _now()}
                e["count"] += 1
                e["last"] = _now()
                agg[key] = e
                if len(agg) > 12:  # keep the store tiny; drop the stalest
                    stalest = min(agg, key=lambda k: agg[k]["last"])
                    del agg[stalest]

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
    """User said yes and named it. Fold the evidence in, then heal -
    including the restore lists: URLs that belong to the new thread's
    story leave every other thread's bring-back list."""
    t = new_thread(g, name, origin="emergent")
    for u in g["untagged"]:
        if u.get("frame") in set(proposal.get("frames", [])):
            feed_embedding(g, t["id"], u.get("vec"))
    reassign_restore(g, t["id"], proposal.get("sample_text", ""))
    heal(g)
    return t


def reassign_restore(g, new_tid, story_text):
    """Restore lists recompute after reclassification: URLs whose domain
    appears in the new thread's evidence move there from any thread that
    absorbed them by mistake."""
    story = (story_text or "").lower()
    if not story:
        return
    new_t = g["threads"].get(new_tid)
    for tid, t in g["threads"].items():
        if tid == new_tid:
            continue
        keep_urls = []
        for u in t.get("recent_urls", []):
            try:
                host = u.split("//", 1)[-1].split("/", 1)[0]
                host = host.removeprefix("www.")
            except Exception:
                keep_urls.append(u)
                continue
            if host.lower() in story:
                if new_t is not None:
                    moved = [x for x in new_t.get("recent_urls", [])
                             if x != u]
                    new_t["recent_urls"] = (moved + [u])[-5:]
                # its chip credit moves too
                agg = t.get("sources_agg") or {}
                if host in agg and new_t is not None:
                    nagg = new_t.setdefault("sources_agg", {})
                    nagg[host] = agg.pop(host)
            else:
                keep_urls.append(u)
        t["recent_urls"] = keep_urls


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


# --- Nodes (documents, people, decisions, blockers, tasks) ----------------

NODE_KINDS = ("document", "person", "decision", "blocker", "task")


def upsert_node(g, tid, kind, label, source="", sticky=False):
    """Attach a node to a thread, or refresh it if it already exists.

    Nodes come from card-time entity extraction and from the user adding
    them by hand. A user-added or user-moved node is STICKY: the classifier
    may never override its thread assignment.
    """
    label = " ".join((label or "").split())[:80]
    if not label or kind not in NODE_KINDS or tid not in g["threads"]:
        return None
    for n in g["nodes"]:
        if n["thread"] == tid and n["label"].lower() == label.lower():
            n["last_seen"] = _now()
            n["seen"] = n.get("seen", 1) + 1
            n["salience"] = min(5.0, n.get("salience", 1.0) + 0.5)
            if source and source not in n["sources"]:
                n["sources"] = (n["sources"] + [source])[-8:]
            if sticky:
                n["sticky"] = True
            return n
    node = {
        "id": f"n{len(g['nodes'])}_{re.sub(r'[^a-z0-9]+', '-', label.lower())[:24]}",
        "thread": tid, "kind": kind, "label": label,
        "pinned": False, "resolved": False, "sticky": bool(sticky),
        "salience": 1.0, "seen": 1,
        "first_seen": _now(), "last_seen": _now(),
        "sources": [source] if source else [],
    }
    g["nodes"].append(node)
    if sticky:
        set_sticky(g, label, tid)
    return node


def get_node(g, nid):
    return next((n for n in g["nodes"] if n["id"] == nid), None)


def set_sticky(g, label, tid):
    """A user statement about where something belongs. Tier 1; permanent
    unless the user says otherwise. The classifier can never override it."""
    g.setdefault("sticky", {})[label.lower().strip()] = tid
    add_anchor(g, tid, label)


def visible_nodes(g, tid, limit=6):
    """The few nodes worth showing: pinned first (human salience override),
    then blockers, then by salience and recency."""
    mine = [n for n in g["nodes"] if n["thread"] == tid]
    mine.sort(key=lambda n: (
        not n.get("pinned"),
        not (n["kind"] == "blocker" and not n.get("resolved")),
        -n.get("salience", 1.0),
        n.get("last_seen", ""),
    ))
    return mine[:limit], mine[limit:]


def merge_threads(g, keep_tid, absorb_tid):
    """Two threads become one. Union of nodes, history, anchors,
    embeddings; the kept thread's name survives."""
    keep, absorb = g["threads"].get(keep_tid), g["threads"].get(absorb_tid)
    if not keep or not absorb or keep_tid == absorb_tid:
        return False
    for n in g["nodes"]:
        if n["thread"] == absorb_tid:
            n["thread"] = keep_tid
    keep["history"] = (absorb["history"] + keep["history"])[-40:]
    for a in absorb["anchors"]:
        add_anchor(g, keep_tid, a)
    keep["recent_embeddings"] = (absorb.get("recent_embeddings", []) +
                                 keep.get("recent_embeddings", []))[-CENTROID_WINDOW:]
    keep["centroid"] = mean_vec(keep["recent_embeddings"])
    if not keep.get("return_point"):
        keep["return_point"] = absorb.get("return_point", "")
    for lbl, t in list(g.get("sticky", {}).items()):
        if t == absorb_tid:
            g["sticky"][lbl] = keep_tid
    if g["meta"].get("active_thread") == absorb_tid:
        g["meta"]["active_thread"] = keep_tid
    g["affinity"].pop(absorb_tid, None)
    del g["threads"][absorb_tid]
    return True


def archive_thread(g, tid):
    """Done. Leaves the board, stops attracting classification, history
    preserved. Not deletion - the dashboard's forget is deletion."""
    t = g["threads"].get(tid)
    if not t:
        return False
    t["status"] = "archived"
    if g["meta"].get("active_thread") == tid:
        g["meta"]["active_thread"] = None
    g["affinity"].pop(tid, None)
    return True


def resume_thread(g, tid):
    """A deliberate human switch: instant, with an affinity head-start so
    the next few frames don't have to re-earn what the person just said."""
    t = g["threads"].get(tid)
    if not t:
        return False
    active = g["meta"].get("active_thread")
    if active and active in g["threads"] and active != tid:
        g["threads"][active]["status"] = "parked"
    t["status"] = "active"
    t["last_seen"] = _now()
    g["meta"]["active_thread"] = tid
    g["affinity"][tid] = SWITCH_MOMENTUM + AFFINITY_GAIN
    add_history(g, tid, "resume", f"resumed deliberately at {_now()}")
    return True


def chips(t, top=4):
    """The apps/domains that composed a thread, recency-weighted.

    A chip exists only because real classified frames back it. Returns
    [{label, count, first, last}] plus a spillover count.
    """
    agg = {k: v for k, v in (t.get("sources_agg") or {}).items()
           if k.lower() not in ("unknown", "hidden")}
    ranked = sorted(agg.items(),
                    key=lambda kv: (kv[1]["last"], kv[1]["count"]),
                    reverse=True)
    shown = [{"label": k, **v} for k, v in ranked[:top]]
    return shown, max(0, len(ranked) - top)


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
    rows = []
    for t in threads:
        row = {k: t[k] for k in
               ("id", "name", "status", "origin", "return_point",
                "last_seen", "salience", "anchors")}
        row["chips"], row["chips_more"] = chips(t)
        rows.append(row)
    return {
        "active": active_tid,
        "threads": rows,
        "untagged_count": len(g["untagged"]),
        "affinity": g["affinity"],
    }
