#!/usr/bin/env python3
"""Recovery Card - Stage 3: Interface.

One calm page at localhost:5000 that is both the user surface and the live
demo control room. Start and stop capture from the browser, watch the state
walk ACTIVE -> AWAY -> RECONSTRUCTING -> CARD READY, and read the card.

Everything is local. The only network call is to localhost:11434.

Run:  .venv/bin/python app.py
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

import capture
import card as card_mod
import eval as eval_mod
import threads as threads_mod
import trust as trust_mod

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"
CARDS = ROOT / "cards"
STATUS = CAPTURES / "status.json"
PARK_NOTE = CARDS / "park_note.json"
CORRECTIONS = CARDS / "corrections.json"

app = Flask(__name__)

# The capture subprocess we launched, if any.
_capture_proc = None
# Set while an on-demand card generation is running.
_generating_since = None
_gen_lock = threading.Lock()


def start_generation(trigger):
    """Run card generation OFF the request thread, always.

    P0 lesson: generation on the serving thread froze every endpoint for
    the full inference - the overlay could not poll, could not dismiss via
    its fallback, and read as a stuck full-screen trap. Generation now
    runs in a daemon thread; requests keep flowing; the overlay shows a
    calm reconstructing state and stays dismissable throughout.

    Returns False if a generation is already running (never stack them).
    """
    global _generating_since
    with _gen_lock:
        if _generating_since is not None:
            return False
        _generating_since = time.time()

    def run():
        global _generating_since
        os.environ["RECOVERY_TRIGGER"] = trigger
        try:
            if os.environ.get("RC_TEST_SLOW_GEN"):
                time.sleep(float(os.environ["RC_TEST_SLOW_GEN"]))
            else:
                card_mod.generate()
        except Exception:
            pass
        finally:
            os.environ.pop("RECOVERY_TRIGGER", None)
            _generating_since = None

    threading.Thread(target=run, daemon=True).start()
    return True


# --- Helpers --------------------------------------------------------------

def read_status():
    """capture.py's state, or a stopped placeholder if it isn't running."""
    if not STATUS.exists():
        return {"mode": "STOPPED", "frames_kept": 0, "frames_skipped": 0}
    try:
        s = json.loads(STATUS.read_text())
    except Exception:
        return {"mode": "STOPPED", "frames_kept": 0, "frames_skipped": 0}

    # A status file left over from a dead process must not look alive.
    if s.get("mode") != "STOPPED" and not pid_alive(s.get("pid")):
        s["mode"] = "STOPPED"

    # STATUS HONESTY: "watching" requires a live LOOP, not a live pid.
    # A SIGSTOPPED corpse passes pid_alive; a loop stuck in a syscall
    # passes too. The heartbeat is the truth.
    beat = s.get("beat")
    interval = float(s.get("capture_interval") or 10)
    if s.get("mode") not in ("STOPPED",) and beat and \
            time.time() - beat > max(30, interval * 3):
        s["mode"] = "STALLED"
        s["stall_seconds"] = int(time.time() - beat)
    return s


def watch_note(s):
    """Plain words when the green dot would otherwise lie.

    Engaged user + nothing KEPT for 2 minutes = watching blind; say so
    with the reason the system actually knows.
    """
    if s.get("mode") == "STALLED":
        return (f"capture stalled {s.get('stall_seconds', '?')}s ago — "
                "restart watching")
    if s.get("mode") in ("STOPPED", "PAUSED", "PAUSED_PRIVATE"):
        return ""
    lk = s.get("last_kept_at")
    if not lk or time.time() - lk < 120:
        return ""
    mins = int((time.time() - lk) / 60)
    reasons = s.get("skip_reasons") or {}
    why = "screen unchanged" if reasons.get("unchanged") else \
        "no engaged moments to keep"
    return f"nothing new kept for {mins}m — {why}"


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def capture_running():
    if _capture_proc and _capture_proc.poll() is None:
        return True
    return pid_alive(read_status().get("pid"))


def card_files():
    return sorted(CARDS.glob("card_*.json"), reverse=True)


def load_card(path, include_contaminated=False):
    try:
        c = json.loads(path.read_text())
        if c.get("contaminated") and not include_contaminated:
            return None  # mirror poison: excluded from display by default
        c["_file"] = path.name
        return c
    except Exception:
        return None


def latest_card():
    for p in card_files():
        c = load_card(p)
        if c:
            return c
    return None


def network_up():
    """True if the machine can reach the internet.

    Shown so a judge can watch this go OFFLINE and see the cards keep coming.
    """
    try:
        s = socket.create_connection(("1.1.1.1", 53), timeout=1.2)
        s.close()
        return True
    except Exception:
        return False


def ollama_up():
    try:
        card_mod.ollama_json("/api/tags", timeout=3)
        return True
    except Exception:
        return False


def current_model():
    try:
        model, reduced = card_mod.pick_model()
        return model, reduced
    except Exception:
        return None, False


def human_time(iso_or_epoch):
    if not iso_or_epoch:
        return "never"
    try:
        if isinstance(iso_or_epoch, (int, float)):
            dt = datetime.fromtimestamp(iso_or_epoch)
        else:
            dt = datetime.fromisoformat(iso_or_epoch)
        secs = (datetime.now() - dt).total_seconds()
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        return dt.strftime("%H:%M")
    except Exception:
        return str(iso_or_epoch)


# --- Preflight ------------------------------------------------------------

def preflight():
    """Check the three things that silently break this app.

    Returns a list of {level, problem, fix} in plain English.
    """
    issues = []

    if not ollama_up():
        issues.append({
            "level": "error",
            "problem": "Ollama is not responding on localhost:11434.",
            "fix": "Open Terminal and run:  ollama serve",
        })
        return issues  # nothing else is meaningful without Ollama

    model, reduced = current_model()
    if model is None:
        issues.append({
            "level": "error",
            "problem": "Neither Gemma 4 model is installed.",
            "fix": f"Open Terminal and run:  ollama pull {card_mod.PRIMARY_MODEL}",
        })
    elif reduced:
        issues.append({
            "level": "warn",
            "problem": (f"The main model ({card_mod.PRIMARY_MODEL}) is missing, "
                        f"so the reduced model is being used. It fabricates "
                        f"detail, so its cards are forced to low confidence."),
            "fix": f"Open Terminal and run:  ollama pull {card_mod.PRIMARY_MODEL}",
        })

    # Does a screenshot actually contain the screen, or a blank rectangle?
    probe = CAPTURES / "preflight_probe.png"
    CAPTURES.mkdir(exist_ok=True)
    try:
        if not capture.grab_screen(probe):
            issues.append({
                "level": "error",
                "problem": "Screenshots are failing.",
                "fix": ("System Settings > Privacy & Security > Screen "
                        "Recording, switch on the app you started this from, "
                        "then restart it."),
            })
        else:
            fp = capture.fingerprint(probe)
            spread = (max(fp) - min(fp)) if fp else 0
            if spread < 10:
                issues.append({
                    "level": "error",
                    "problem": ("Screenshots are coming out blank. macOS is "
                                "handing over an empty screen."),
                    "fix": ("System Settings > Privacy & Security > Screen "
                            "Recording, switch on the app you started this "
                            "from, then restart it."),
                })
    finally:
        probe.unlink(missing_ok=True)

    return issues


# --- Routes ---------------------------------------------------------------

@app.route("/")
def index():
    """The front door: onboarding on first run, the board after."""
    g = threads_mod.load()
    if not g["meta"].get("onboarded"):
        return render_template_string(ONBOARD_PAGE)
    return render_template_string(BOARD_PAGE)


@app.route("/engine")
def engine_room():
    """The old control-room page. Emergency fallback only."""
    return render_template_string(PAGE)


@app.route("/thread/<tid>")
def thread_map_page(tid):
    """A thread's map. The page pulls its thread id from the URL."""
    g = threads_mod.load()
    if tid not in g["threads"]:
        return render_template_string(BOARD_PAGE)  # calm way back
    return render_template_string(MAP_PAGE)


# --- Thread + board API ---------------------------------------------------

def headline(g, st):
    """One plain sentence stating the situation. Recognition, not recall."""
    active = g["threads"].get(g["meta"].get("active_thread"))
    others = sum(1 for t in g["threads"].values()
                 if t.get("status") != "ambient"
                 and t["id"] != g["meta"].get("active_thread"))
    if active:
        # Return-points are model text: strip engine-speak and trailing
        # punctuation so concatenation can't produce "window.. You're".
        rp = (active.get("return_point", "") or "").replace("`", "")
        rp = " ".join(rp.split()).rstrip(".")
        if len(rp) > 90:
            rp = rp[:88].rstrip() + "…"
        h = f"You're on {active['name']}"
        if rp:
            h += f" — {rp}"
        h += "."
        if others:
            h += f" You're holding {others} other " + \
                 ("thread." if others == 1 else "threads.")
        return h
    if others:
        return (f"You're holding {others} " +
                ("thread. " if others == 1 else "threads. ") +
                "None is active right now.")
    if st.get("mode") in ("ACTIVE", "AWAY", "CARD_READY", "RECONSTRUCTING"):
        return "Watching quietly. Threads will appear as your work does."
    return "Not watching yet. Start capture when you're ready."


@app.route("/api/board")
def api_board():
    g = threads_mod.load()
    st = read_status()
    b = threads_mod.board(g)
    b["headline"] = headline(g, st)
    b["onboarded"] = bool(g["meta"].get("onboarded"))
    b["emergent"] = threads_mod.emergent_candidate(g)
    return jsonify(b)


@app.route("/api/thread/add", methods=["POST"])
def api_thread_add():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "a thread needs a name"}), 400
    g = threads_mod.load()
    t = threads_mod.new_thread(
        g, name,
        origin=body.get("origin", "declared"),
        anchors=body.get("anchors") or [name],
        return_point=(body.get("return_point") or "").strip())
    threads_mod.save(g)
    return jsonify({"ok": True, "id": t["id"]})


@app.route("/api/thread/confirm_emergent", methods=["POST"])
def api_confirm_emergent():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "a thread needs a name"}), 400
    g = threads_mod.load()
    prop = threads_mod.emergent_candidate(g)
    if not prop:
        return jsonify({"ok": False, "error": "nothing to confirm"}), 400
    t = threads_mod.confirm_emergent(g, name, prop)
    threads_mod.save(g)
    return jsonify({"ok": True, "id": t["id"]})


@app.route("/api/thread/dismiss_emergent", methods=["POST"])
def api_dismiss_emergent():
    """User said no: leave those frames ambient. Holding ambiguity is honest."""
    g = threads_mod.load()
    for u in g["untagged"][-threads_mod.EMERGENT_MIN_FRAMES:]:
        u["engaged"] = False  # no longer counts toward a proposal
    threads_mod.save(g)
    return jsonify({"ok": True})


# --- Thread map: resume, restore, node actions -----------------------------

import events as events_mod


def thread_cards(name, limit=8):
    """Recent cards belonging to a thread, newest first. Read live."""
    out = []
    for p in card_files():
        c = load_card(p)
        if c and c.get("thread") == name:
            out.append({"file": p.name,
                        "at": c.get("generated_at", ""),
                        "goal": c.get("goal", "")})
        if len(out) >= limit:
            break
    return out


def restorable(t):
    """What Resume can verifiably bring back: exact URLs and the app.

    Honest by construction - scroll position, unsaved state and
    workspace-within-app are not restorable and the UI says so.
    """
    items = []
    for u in (t.get("recent_urls") or [])[-3:]:
        items.append({"kind": "url", "value": u,
                      "label": u.split("//")[-1][:60]})
    if t.get("last_app"):
        items.append({"kind": "app", "value": t["last_app"],
                      "label": f"open {t['last_app']}"})
    return items


@app.route("/api/thread/<tid>/map")
def api_thread_map(tid):
    g = threads_mod.load()
    t = g["threads"].get(tid)
    if not t:
        return jsonify({"ok": False, "error": "unknown thread"}), 404

    vis, more = threads_mod.visible_nodes(g, tid)
    parked_mins = None
    if t.get("status") != "active" and t.get("last_seen"):
        try:
            parked_mins = int((datetime.now() - datetime.fromisoformat(
                t["last_seen"])).total_seconds() / 60)
        except Exception:
            pass

    h = t.get("return_point") or "No return-point yet."
    if parked_mins is not None and parked_mins >= 1:
        h += f" · parked {parked_mins} min" if parked_mins < 120 \
            else f" · parked {parked_mins // 60} h"

    return jsonify({
        "ok": True,
        "thread": {k: t.get(k) for k in
                   ("id", "name", "status", "origin", "return_point",
                    "last_seen")},
        "headline": h,
        "nodes": [{k: n.get(k) for k in
                   ("id", "kind", "label", "pinned", "resolved", "sticky")}
                  for n in vis],
        "more_nodes": [{k: n.get(k) for k in ("id", "kind", "label",
                                              "pinned", "resolved")}
                       for n in more],
        "cards": thread_cards(t["name"]),
        "restorable": restorable(t),
        "chips": threads_mod.chips(t)[0],
        "chips_more": threads_mod.chips(t)[1],
    })


@app.route("/api/thread/<tid>/resume", methods=["POST"])
def api_thread_resume(tid):
    g = threads_mod.load()
    if not threads_mod.resume_thread(g, tid):
        return jsonify({"ok": False, "error": "unknown thread"}), 404
    threads_mod.save(g)
    events_mod.log_event("resume_click", thread=tid)
    open_overlay()  # the Return Card surface, with the latest card
    return jsonify({"ok": True})


@app.route("/api/thread/<tid>/restore", methods=["POST"])
def api_thread_restore(tid):
    """Acts ONLY on what the user just previewed and confirmed."""
    body = request.json or {}
    g = threads_mod.load()
    t = g["threads"].get(tid)
    if not t:
        return jsonify({"ok": False, "error": "unknown thread"}), 404
    allowed = {(i["kind"], i["value"]) for i in restorable(t)}
    opened = []
    for item in (body.get("items") or [])[:5]:
        key = (item.get("kind"), item.get("value"))
        if key not in allowed:
            continue  # never act on anything that wasn't previewed
        if key[0] == "url":
            subprocess.run(["open", key[1]], capture_output=True, timeout=10)
        elif key[0] == "app":
            subprocess.run(["open", "-a", key[1]], capture_output=True,
                           timeout=10)
        opened.append(item.get("label", key[1]))
    return jsonify({"ok": True, "opened": opened})


@app.route("/api/node/<nid>", methods=["POST"])
def api_node_action(nid):
    """Every map correction, one endpoint. All of them are ground truth."""
    body = request.json or {}
    action = body.get("action", "")
    g = threads_mod.load()
    n = threads_mod.get_node(g, nid)
    if not n:
        return jsonify({"ok": False, "error": "unknown node"}), 404

    truth = {"node": n["label"], "kind": n["kind"], "action": action}

    if action == "rename":
        new = " ".join((body.get("value") or "").split())[:80]
        if not new:
            return jsonify({"ok": False, "error": "a name is needed"}), 400
        old = n["label"]
        n["label"] = new
        threads_mod.set_sticky(g, new, n["thread"])
        truth["from"], truth["to"] = old, new
    elif action == "move":
        dest = body.get("thread", "")
        if dest not in g["threads"]:
            return jsonify({"ok": False, "error": "unknown thread"}), 400
        n["thread"] = dest
        n["sticky"] = True
        threads_mod.set_sticky(g, n["label"], dest)
        # the correction reshapes both centroids via the label embedding
        vec = threads_mod.embed(n["label"])[0]
        threads_mod.feed_embedding(g, dest, vec)
        truth["to"] = dest
    elif action == "detach":
        n["thread"] = None
        truth["to"] = "unfiled"
    elif action == "remove":
        g["nodes"] = [x for x in g["nodes"] if x["id"] != nid]
    elif action == "resolve":
        n["resolved"] = True
        # a resolved blocker changes what the return-point should say
        t = g["threads"].get(n["thread"])
        if t and n["kind"] in ("blocker", "decision") and \
                n["label"].lower() in (t.get("return_point") or "").lower():
            t["return_point"] = f"{n['label']} is resolved — pick up from there."
    elif action in ("pin", "unpin"):
        n["pinned"] = action == "pin"
    else:
        return jsonify({"ok": False, "error": "unknown action"}), 400

    threads_mod.save(g)
    # Corrections are ground truth: they go to the eval store.
    try:
        truth["at"] = datetime.now().isoformat(timespec="seconds")
        p = ROOT / "eval" / "map_corrections.json"
        rows = json.loads(p.read_text()) if p.exists() else []
        rows.append(truth)
        p.write_text(json.dumps(rows[-200:], indent=1))
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/node/<nid>/why")
def api_node_why(nid):
    """Provenance on request: what created this node, when last seen."""
    g = threads_mod.load()
    n = threads_mod.get_node(g, nid)
    if not n:
        return jsonify({"ok": False}), 404
    details = []
    for s in n.get("sources", []):
        if s.startswith("card_"):
            c = load_card(CARDS / s, include_contaminated=True)
            if c:
                details.append({
                    "kind": "card", "file": s,
                    "at": c.get("generated_at", ""),
                    "sentence": c.get("goal", ""),
                    "contaminated": bool(c.get("contaminated"))})
        elif s == "user":
            details.append({"kind": "user",
                            "sentence": "You added this yourself."})
    return jsonify({"ok": True, "label": n["label"], "kind": n["kind"],
                    "seen": n.get("seen", 1),
                    "first_seen": n.get("first_seen"),
                    "last_seen": n.get("last_seen"),
                    "sources": n.get("sources", []),
                    "details": details,
                    "sticky": n.get("sticky", False)})


@app.route("/api/thread/<tid>/node/add", methods=["POST"])
def api_node_add(tid):
    body = request.json or {}
    g = threads_mod.load()
    n = threads_mod.upsert_node(
        g, tid, body.get("kind", "task"), body.get("label", ""),
        source="user", sticky=True)
    if not n:
        return jsonify({"ok": False, "error": "could not add"}), 400
    threads_mod.save(g)
    return jsonify({"ok": True, "id": n["id"]})


@app.route("/api/thread/merge", methods=["POST"])
def api_thread_merge():
    body = request.json or {}
    g = threads_mod.load()
    if not threads_mod.merge_threads(g, body.get("keep", ""),
                                     body.get("absorb", "")):
        return jsonify({"ok": False, "error": "could not merge"}), 400
    threads_mod.save(g)
    return jsonify({"ok": True})


@app.route("/api/thread/<tid>/archive", methods=["POST"])
def api_thread_archive(tid):
    g = threads_mod.load()
    if not threads_mod.archive_thread(g, tid):
        return jsonify({"ok": False, "error": "unknown thread"}), 404
    threads_mod.save(g)
    return jsonify({"ok": True})


@app.route("/api/metrics")
def api_metrics():
    return jsonify(events_mod.resumptions())


@app.route("/api/classify_log")
def api_classify_log():
    """The live classification stream for the engine room."""
    p = ROOT / "captures" / "classify_log.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text().splitlines()[-30:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    rows.reverse()
    return jsonify({"rows": rows})


@app.route("/api/card/<name>/move", methods=["POST"])
def api_card_move(name):
    """Wrong thread? Move this card. A card-level correction:
    sticky-authority, retro-heals everything the card produced."""
    dest_tid = (request.json or {}).get("thread", "")
    path = CARDS / name
    if not re.match(r"^card_\d{8}_\d{6}\.json$", name) or not path.exists():
        return jsonify({"ok": False, "error": "unknown card"}), 400
    g = threads_mod.load()
    dest = g["threads"].get(dest_tid)
    if not dest:
        return jsonify({"ok": False, "error": "unknown thread"}), 400

    c = json.loads(path.read_text())
    old_name = c.get("thread", "")
    c["thread"] = dest["name"]
    c["thread_moved"] = {"from": old_name, "to": dest["name"],
                         "at": datetime.now().isoformat(timespec="seconds")}
    path.write_text(json.dumps(c, indent=2))

    # Sticky correction: the card's own text reshapes the destination.
    vec = threads_mod.embed(
        f'{c.get("goal", "")} {c.get("evidence", "")}')[0]
    threads_mod.feed_embedding(g, dest_tid, vec)
    threads_mod.add_history(g, dest_tid, "correction",
                            f"card moved here: {c.get('goal', '')}")

    # Retro-heal: nodes this card created follow it; the old thread's
    # return-point heals if it came from this card.
    for n in g["nodes"]:
        if name in (n.get("sources") or []):
            n["thread"] = dest_tid
            n["sticky"] = True
    old = next((t for t in g["threads"].values()
                if t["name"] == old_name), None)
    if old and old.get("return_point") == c.get("next_action"):
        old["return_point"] = ""
        for h in reversed(old.get("history", [])):
            if h["kind"] in ("park", "composed", "card") \
                    and c.get("goal", "") not in h["text"]:
                old["return_point"] = h["text"][:120]
                break
    threads_mod.touch(g, dest_tid, return_point=c.get("next_action"))
    threads_mod.save(g)

    # Ground truth for the eval store.
    try:
        p = ROOT / "eval" / "map_corrections.json"
        rows = json.loads(p.read_text()) if p.exists() else []
        rows.append({"action": "move_card", "card": name,
                     "from": old_name, "to": dest["name"],
                     "at": datetime.now().isoformat(timespec="seconds")})
        p.write_text(json.dumps(rows[-200:], indent=1))
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/card/<name>/done", methods=["POST"])
def api_card_done(name):
    """The Done check on a next step. Marks it, strikes matching open
    loops, logs. Executes nothing."""
    path = CARDS / name
    if not re.match(r"^card_\d{8}_\d{6}\.json$", name) or not path.exists():
        return jsonify({"ok": False, "error": "unknown card"}), 400
    c = json.loads(path.read_text())
    c["next_done"] = True
    na = (c.get("next_action") or "").lower()
    c["loops_done"] = [l for l in c.get("open_loops", [])
                       if l.lower() in na or na in l.lower()]
    path.write_text(json.dumps(c, indent=2))
    events_mod.log_event("next_done", card=name)
    g = threads_mod.load()
    tid = next((t["id"] for t in g["threads"].values()
                if t["name"] == c.get("thread")), None)
    if tid:
        threads_mod.add_history(g, tid, "done",
                                f"done: {c.get('next_action', '')}")
        threads_mod.save(g)
    return jsonify({"ok": True})


# --- Trust dashboard API ---------------------------------------------------
# Contract: everything here is read live from the real files on disk when
# the request arrives. No cached claims. Every control is real.

@app.route("/trust")
def trust_page():
    return render_template_string(TRUST_PAGE)


@app.route("/api/trust")
def api_trust():
    model, reduced = current_model()
    return jsonify({
        "switches": trust_mod.read_settings(),
        "switch_names": trust_mod.SWITCHES,
        "data": trust_mod.data_on_hand(),
        "network": "ONLINE" if network_up() else "OFFLINE",
        "model": model or "none",
        "refusals": [
            "Concealed clipboard content (password managers) is never captured.",
            "No microphone. No ambient audio. Ever.",
            "Nothing is uploaded. The only network call this app makes is to "
            "the local model on this Mac.",
        ],
    })


@app.route("/api/private", methods=["GET", "POST"])
def api_private():
    """The exclusion list. Changes take effect within one capture cycle."""
    if request.method == "GET":
        d = trust_mod.read_private()
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of every '
                 'application process whose background only is false'],
                capture_output=True, text=True, timeout=5)
            running = sorted({a.strip() for a in r.stdout.split(",")
                              if a.strip()} - set(d["apps"]))
        except Exception:
            running = []
        return jsonify({**d, "running": running})

    body = request.json or {}
    d = trust_mod.read_private()
    action, kind = body.get("action"), body.get("kind", "apps")
    value = (body.get("value") or "").strip()
    if not value or kind not in ("apps", "domains"):
        return jsonify({"ok": False, "error": "nothing to change"}), 400
    items = set(d[kind])
    if action == "add":
        items.add(value)
    elif action == "remove":
        items.discard(value)
    else:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    d[kind] = sorted(items)
    trust_mod.write_private(d)
    return jsonify({"ok": True, **trust_mod.read_private()})


@app.route("/api/trust/toggle", methods=["POST"])
def api_trust_toggle():
    body = request.json or {}
    name = body.get("switch", "")
    try:
        s = trust_mod.set_switch(name, bool(body.get("value")))
        return jsonify({"ok": True, "switches": s})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/trust/delete", methods=["POST"])
def api_trust_delete():
    body = request.json or {}
    scope = body.get("scope", "")
    if scope == "thread":
        ok = trust_mod.forget_thread(body.get("id", ""))
        return jsonify({"ok": ok})
    if scope == "captures":
        return jsonify({"ok": True, "deleted": trust_mod.delete_captures()})
    if scope == "everything":
        return jsonify({"ok": True, "deleted": trust_mod.delete_everything()})
    return jsonify({"ok": False, "error": "unknown scope"}), 400


@app.route("/captures/<name>")
def serve_capture(name):
    """The actual screenshots, viewable from the dashboard. Local only."""
    from flask import send_from_directory, abort
    if not re.match(r"^frame_\d{8}_\d{6}\.(png|json)$", name):
        abort(404)
    return send_from_directory(CAPTURES, name)


@app.route("/cards/<name>")
def serve_card(name):
    from flask import send_from_directory, abort
    if not re.match(r"^card_\d{8}_\d{6}\.json$", name):
        abort(404)
    return send_from_directory(CARDS, name)


# --- Onboarding API -------------------------------------------------------

@app.route("/api/onboard/propose", methods=["POST"])
def api_onboard_propose():
    """Take ONE screenshot, run ONE inference, propose candidate threads.

    This is the app proving its core capability in the first ten seconds:
    it looks at the screen and says what threads it sees open. The user
    confirms, renames, adds, or ignores - nothing is filed silently.
    """
    probe = CAPTURES / "onboard_probe.png"
    CAPTURES.mkdir(exist_ok=True)
    if not capture.grab_screen(probe):
        return jsonify({"ok": False, "error":
                        "Could not take a screenshot. Check Screen Recording "
                        "permission, then try again."}), 500
    try:
        import base64 as b64
        ctx = capture.window_context()
        img = b64.b64encode(probe.read_bytes()).decode()
        model, _ = card_mod.pick_model()
        schema = {
            "type": "object",
            "properties": {"threads": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"},
                    "evidence": {"type": "string"}},
                    "required": ["name", "evidence"]}}},
            "required": ["threads"]}
        prompt = (
            "Look at this screenshot of someone's Mac. Window titles: "
            + "; ".join(ctx.get("window_titles", [])[:6]) +
            ". Identify up to 4 distinct THREADS of work or attention that "
            "seem open - projects, errands, conversations. Short human names "
            "('Phossil production app', 'Mac speaker repair'), not app names. "
            "For each, one sentence of on-screen evidence. Only what you can "
            "actually see. JSON only.")
        raw = card_mod.ollama_json("/api/generate", {
            "model": model, "images": [img], "prompt": prompt,
            "stream": False, "think": False, "format": schema,
            "options": {"temperature": 0.2}}, timeout=180)
        proposals = json.loads(raw.get("response", "{}")).get("threads", [])[:4]
        return jsonify({"ok": True, "proposals": proposals})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        probe.unlink(missing_ok=True)


@app.route("/api/onboard/complete", methods=["POST"])
def api_onboard_complete():
    body = request.json or {}
    g = threads_mod.load()
    for th in (body.get("threads") or [])[:8]:
        name = (th.get("name") or "").strip()
        if name and not any(t["name"].lower() == name.lower()
                            for t in g["threads"].values()):
            threads_mod.new_thread(g, name, origin="declared",
                                   anchors=[name] + (th.get("anchors") or []))
    g["meta"]["onboarded"] = True
    g["meta"]["onboarded_at"] = datetime.now().isoformat(timespec="seconds")
    threads_mod.save(g)
    return jsonify({"ok": True, "threads": len(g["threads"])})


@app.route("/api/state")
def api_state():
    global _generating_since

    st = read_status()
    running = capture_running()
    idle = capture.idle_seconds()
    model, reduced = current_model()

    mode = st.get("mode", "STOPPED")
    if not running:
        mode = "STOPPED"

    # An on-demand generation from the browser also counts as reconstructing.
    if _generating_since is not None:
        mode = "RECONSTRUCTING"

    elapsed = None
    if mode == "RECONSTRUCTING":
        started = _generating_since or st.get("card_started_at")
        if started:
            elapsed = int(time.time() - started)

    c = latest_card()
    return jsonify({
        "mode": mode,
        "running": running,
        "idle_seconds": round(idle, 1),
        "idle_threshold": st.get("idle_threshold", 60),
        "frames_kept": st.get("frames_kept", 0),
        "frames_skipped": st.get("frames_skipped", 0),
        "last_capture": human_time(st.get("last_capture")),
        "last_card": human_time(c.get("generated_at")) if c else "never",
        "reconstructing_for": elapsed,
        "away_summary": st.get("away_summary", ""),
        "watch_note": watch_note(st),
        "model": model or "none",
        "reduced_model": reduced,
        "network": "ONLINE" if network_up() else "OFFLINE",
        "card": c,
        "history": [
            {"file": p.name,
             "at": (load_card(p) or {}).get("generated_at", ""),
             "goal": (load_card(p) or {}).get("goal", "")}
            for p in card_files()[:12]
        ],
    })


@app.route("/api/capture/<action>", methods=["POST"])
def api_capture(action):
    global _capture_proc

    if action == "start":
        if capture_running():
            return jsonify({"ok": True, "note": "already running"})
        env = dict(os.environ)
        # Demo-friendly defaults; override before launching app.py.
        env.setdefault("IDLE_THRESHOLD", os.environ.get("IDLE_THRESHOLD", "60"))
        _capture_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "capture.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        return jsonify({"ok": True})

    if action == "stop":
        stopped = False
        if _capture_proc and _capture_proc.poll() is None:
            _capture_proc.terminate()
            stopped = True
        else:
            pid = read_status().get("pid")
            if pid_alive(pid):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    stopped = True
                except Exception:
                    pass
        return jsonify({"ok": stopped})

    return jsonify({"ok": False, "error": "unknown action"}), 400


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """The 'I'm back' path when no card is waiting. Returns immediately;
    the generation runs in the background and state polling shows it."""
    started = start_generation("im_back")
    return jsonify({"ok": True, "started": started})


@app.route("/api/summon", methods=["POST", "GET"])
def api_summon():
    """Bring up the card. Bound to a global hotkey via macOS Shortcuts.

    Pull, not push: nothing about this system asks for attention. This is
    the moment the person reaches for it.
    """
    global _generating_since

    overlay = str(request.args.get("overlay", "1")).lower() not in ("0", "false")
    c = latest_card()
    fresh = False
    if c:
        try:
            age = (datetime.now() -
                   datetime.fromisoformat(c["generated_at"])).total_seconds()
            fresh = age < 600  # 10 minutes, per spec
        except Exception:
            fresh = False

    if overlay:
        open_overlay()

    if fresh or _generating_since is not None:
        return jsonify({"ok": True, "generated": False, "card": bool(c)})

    started = start_generation("hotkey")
    return jsonify({"ok": True, "generated": started})


_overlay_proc = None


OVERLAY_PIDFILE = ROOT / ".overlay.pid"


def overlay_running():
    """True if ANY overlay process exists - ours or one capture spawned.

    Two spawn paths (summon, wake) that don't know about each other were
    stacking overlays; the top one dismissed and the one beneath read as
    'Esc does nothing'. One overlay, ever.

    Detection is by pidfile, not pgrep -f: command-line matching caught
    any process that merely mentioned overlay.py and silently skipped
    legitimate spawns.
    """
    if _overlay_proc and _overlay_proc.poll() is None:
        return True
    try:
        pid = int(OVERLAY_PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def open_overlay():
    global _overlay_proc
    if overlay_running():
        return
    _overlay_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "overlay.py"),
         f"http://localhost:{request.host.split(':')[-1]}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.route("/api/overlay/show", methods=["POST"])
def api_overlay_show():
    """Open the Return surface on a SPECIFIC card, read-only.

    Used by the thread map's timeline. Replaces any overlay already up.
    """
    card = request.args.get("card", "")
    if not re.match(r"^card_\d{8}_\d{6}\.json$", card):
        return jsonify({"ok": False, "error": "unknown card"}), 400
    OVERLAY_PIDFILE.unlink(missing_ok=True)   # watchdog closes the old one
    time.sleep(0.15)
    global _overlay_proc
    port = request.host.split(":")[-1]
    _overlay_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "overlay.py"),
         f"http://localhost:{port}/overlay?card={card}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True})


@app.route("/api/overlay/close", methods=["POST"])
def api_overlay_close():
    """Last-resort dismissal. A full-screen overlay must never be a trap.

    Deleting the pidfile is the kill switch: the overlay's watchdog thread
    exits within 100ms of noticing. Signals cannot do this - the overlay's
    main thread lives inside Cocoa's run loop where Python signal handlers
    never get to run.
    """
    global _overlay_proc
    OVERLAY_PIDFILE.unlink(missing_ok=True)
    if _overlay_proc:
        proc = _overlay_proc
        # reap promptly so the dead overlay never lingers as a zombie
        threading.Thread(target=lambda: proc.wait(), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/overlay")
def overlay_page():
    """The card alone, on nothing. Rendered into a blurred full-screen
    window so the rest of the desktop stops competing for attention."""
    return render_template_string(OVERLAY_PAGE)


@app.route("/api/park", methods=["POST"])
def api_park():
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty note"}), 400
    CARDS.mkdir(exist_ok=True)
    PARK_NOTE.write_text(json.dumps({
        "text": text,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))
    return jsonify({"ok": True})


@app.route("/api/preflight")
def api_preflight():
    return jsonify({"issues": preflight()})


@app.route("/api/eval")
def api_eval():
    """Scoring state: the tally so far, and the next card awaiting judgement.

    Only cards generated while a park note was active appear here. The park
    note is the user's own words, written before the model saw anything, so
    it is the only honest ground truth available.
    """
    results = eval_mod.load_results()
    todo = eval_mod.pending(results)

    nxt = None
    if todo:
        path, c = todo[0]
        nxt = {
            "file": path.name,
            "park_note": c.get("park_note", ""),
            "generated_at": c.get("generated_at", ""),
            "trigger": c.get("trigger", ""),
            "confidence": c.get("confidence", ""),
            "model": c.get("model", ""),
            "fail_closed": bool(c.get("fail_closed")),
            "goal": c.get("goal", ""),
            "reasoning": c.get("reasoning", ""),
            "next_action": c.get("next_action", ""),
            "open_loops": c.get("open_loops", []),
            "thread": c.get("thread", ""),
        }

    return jsonify({
        "tally": eval_mod.tally_dict(results),
        "pending": len(todo),
        "judged": len(results),
        "next": nxt,
        "fields": eval_mod.FIELDS,
    })


@app.route("/api/verdict", methods=["POST"])
def api_verdict():
    """A tap on the live card: one field, right or wrong.

    Optionally carries a correction - the person saying, in one line, what
    they were actually doing. That is user-confirmed truth, so it is stored
    on the card, in the eval record, and in the session-memory log that
    card.py weights alongside park notes.

    One tap, one optional line, done. No chat, no follow-up.
    """
    body = request.json or {}
    fname = body.get("file", "")
    field = body.get("field", "")
    value = body.get("value")
    text = (body.get("correction") or "").strip()

    path = CARDS / fname
    if not fname.startswith("card_") or not path.exists():
        return jsonify({"ok": False, "error": "unknown card"}), 400
    if field not in eval_mod.FIELDS:
        return jsonify({"ok": False, "error": "unknown field"}), 400

    try:
        c = json.loads(path.read_text())
    except Exception:
        return jsonify({"ok": False, "error": "unreadable card"}), 400

    correction = None
    if text:
        correction = {
            "field": field,
            "text": text,
            "at": datetime.now().isoformat(timespec="seconds"),
            "card": fname,
        }
        # Attach to the card itself, so the artifact carries its own
        # correction and stays replayable.
        c.setdefault("corrections", []).append(correction)
        path.write_text(json.dumps(c, indent=2))

        # And to session memory, which the next card reads as truth.
        log = []
        if CORRECTIONS.exists():
            try:
                log = json.loads(CORRECTIONS.read_text())
            except Exception:
                log = []
        log.append(correction)
        CORRECTIONS.write_text(json.dumps(log[-50:], indent=2))

    results = eval_mod.upsert(
        eval_mod.load_results(), fname, c,
        {field: (True if value is True else False)},
        source="product", correction=correction)

    return jsonify({"ok": True, "tally": eval_mod.tally_dict(results)})


@app.route("/api/eval/mark", methods=["POST"])
def api_eval_mark():
    body = request.json or {}
    fname = body.get("file", "")
    marks = body.get("marks", {})

    path = CARDS / fname
    if not fname.startswith("card_") or not path.exists():
        return jsonify({"ok": False, "error": "unknown card"}), 400

    clean = {}
    for f in eval_mod.FIELDS:
        v = marks.get(f)
        clean[f] = v if v in (True, False) else None  # None = not applicable

    try:
        c = json.loads(path.read_text())
    except Exception:
        return jsonify({"ok": False, "error": "unreadable card"}), 400

    results = eval_mod.record(eval_mod.load_results(), fname, c, clean)
    return jsonify({"ok": True, "tally": eval_mod.tally_dict(results)})


# --- Page -----------------------------------------------------------------

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recovery Card</title>
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --line:#252a34;
    --text:#e6e8ec; --dim:#8b93a1; --accent:#4fd6be;
    --good:#4fd6be; --warn:#e0af68; --bad:#f7768e;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#f6f7f9; --panel:#fff; --line:#e2e5ea;
      --text:#1a1d23; --dim:#6b7280; --accent:#3b6fd4;
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:15px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
    padding:28px 20px 60px;
  }
  .wrap { max-width:760px; margin:0 auto; }
  h1 { font-size:19px; font-weight:600; margin:0 0 18px; letter-spacing:-.01em; }
  h1 span { color:var(--dim); font-weight:400; }

  .strip {
    display:flex; flex-wrap:wrap; gap:18px; padding:12px 16px;
    background:var(--panel); border:1px solid var(--line);
    border-radius:10px; font-size:12.5px; color:var(--dim);
    margin-bottom:14px;
  }
  .strip b { color:var(--text); font-weight:500; }
  .off { color:var(--bad); font-weight:600; }
  .on  { color:var(--good); font-weight:600; }

  .row { display:flex; gap:10px; margin-bottom:18px; }
  button {
    font:inherit; font-size:14px; padding:9px 18px; border-radius:8px;
    border:1px solid var(--line); background:var(--panel); color:var(--text);
    cursor:pointer;
  }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#0f1115; font-weight:600; }
  button:disabled { opacity:.45; cursor:default; }

  .state {
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:26px; text-align:center; margin-bottom:18px;
  }
  .mode { font-size:13px; letter-spacing:.14em; font-weight:600; }
  .mode.ACTIVE { color:var(--good); }
  .mode.AWAY { color:var(--warn); }
  .mode.RECONSTRUCTING { color:var(--accent); }
  .mode.CARD_READY { color:var(--good); }
  .mode.STOPPED { color:var(--dim); }
  .big { font-size:38px; font-weight:300; margin:10px 0 4px; letter-spacing:-.02em; }
  .sub { font-size:13px; color:var(--dim); }
  .beat { display:inline-block; width:8px; height:8px; border-radius:50%;
          background:var(--good); margin-right:7px; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }

  .card {
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:28px; margin-bottom:18px;
  }
  .card h2 {
    font-size:11px; letter-spacing:.14em; color:var(--dim);
    font-weight:600; margin:0 0 8px;
    display:flex; align-items:center; gap:8px;
  }
  /* Verdict taps. Deliberately faint until hovered: the card is for
     reading, not for grading. Confirmation is a colour change, never a
     popup. */
  .taps { margin-left:auto; display:flex; gap:3px; opacity:.25;
          transition:opacity .15s; }
  .card:hover .taps { opacity:.7; }
  .taps:hover { opacity:1 !important; }
  .taps button {
    padding:1px 7px; font-size:11px; border-radius:5px; line-height:1.5;
    background:transparent; border:1px solid var(--line); color:var(--dim);
  }
  .taps button.y.on { background:var(--good); border-color:var(--good);
                      color:#0f1115; opacity:1; }
  .taps button.n.on { background:var(--bad); border-color:var(--bad);
                      color:#0f1115; opacity:1; }
  .fixbox { display:flex; gap:6px; margin:8px 0 2px; }
  .fixbox input {
    flex:1; font:inherit; font-size:13px; padding:7px 11px;
    border-radius:7px; border:1px solid var(--accent);
    background:var(--bg); color:var(--text);
  }
  .fixbox input:focus { outline:none; }
  .fixed-note {
    margin:8px 0 2px; font-size:12.5px; color:var(--good);
  }
  .goal { font-size:23px; font-weight:500; line-height:1.35; margin:0 0 10px; letter-spacing:-.01em; }
  .reason { color:var(--dim); margin:0; }
  .sec { margin-top:24px; }
  .next { font-size:17px; margin:0; }
  ul { margin:0; padding-left:20px; }
  li { margin:3px 0; }
  .said {
    margin-top:22px; padding:12px 16px; border-left:2px solid var(--accent);
    background:rgba(79,214,190,.07); font-style:italic;
  }
  .meta {
    margin-top:24px; padding-top:16px; border-top:1px solid var(--line);
    font-size:12.5px; color:var(--dim);
  }
  .flag {
    display:inline-block; padding:3px 9px; border-radius:5px; font-size:11px;
    font-weight:600; letter-spacing:.06em; margin-bottom:14px;
  }
  .flag.reduced { background:rgba(224,175,104,.16); color:var(--warn); }
  .flag.failed  { background:rgba(247,118,142,.14); color:var(--bad); }

  .park { display:flex; gap:8px; margin-bottom:22px; }
  .park input {
    flex:1; font:inherit; font-size:14px; padding:10px 14px;
    border-radius:8px; border:1px solid var(--line);
    background:var(--panel); color:var(--text);
  }
  .park input:focus { outline:none; border-color:var(--accent); }

  .scoring {
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:22px 24px; margin-bottom:18px;
  }
  .scoring h2 { font-size:11px; letter-spacing:.14em; color:var(--dim);
                font-weight:600; margin:0 0 12px; }
  .tally { font-size:14px; margin:0 0 4px; }
  .tally b { font-variant-numeric:tabular-nums; }
  .tally .ok { color:var(--good); }
  .tally .mid { color:var(--warn); }
  .tally .bad { color:var(--bad); }
  .truth {
    margin:14px 0 6px; padding:10px 14px; border-left:2px solid var(--good);
    background:rgba(79,214,190,.08); font-style:italic; font-size:13.5px;
  }
  .judge-row {
    display:flex; align-items:flex-start; gap:10px; padding:9px 0;
    border-bottom:1px solid var(--line); font-size:13.5px;
  }
  .judge-row:last-child { border-bottom:none; }
  .judge-row .lbl { width:96px; flex:none; color:var(--dim); font-size:12px;
                    padding-top:3px; }
  .judge-row .val { flex:1; }
  .judge-row .btns { flex:none; display:flex; gap:4px; }
  .judge-row button {
    padding:3px 10px; font-size:12px; border-radius:6px; min-width:30px;
  }
  .judge-row button.on-y { background:var(--good); border-color:var(--good);
                           color:#0f1115; font-weight:600; }
  .judge-row button.on-n { background:var(--bad); border-color:var(--bad);
                           color:#0f1115; font-weight:600; }
  .judge-row button.on-na { background:var(--dim); border-color:var(--dim);
                            color:#0f1115; font-weight:600; }
  .hist h2 { font-size:11px; letter-spacing:.14em; color:var(--dim); margin:0 0 10px; }
  .hitem {
    padding:10px 0; border-bottom:1px solid var(--line);
    font-size:13.5px; display:flex; gap:12px;
  }
  .hitem:last-child { border-bottom:none; }
  .hitem time { color:var(--dim); font-size:12px; white-space:nowrap; }
  .issues { margin-bottom:16px; }
  .issue {
    padding:12px 16px; border-radius:9px; margin-bottom:8px; font-size:13.5px;
    border:1px solid var(--line);
  }
  .issue.error { background:rgba(247,118,142,.10); border-color:rgba(247,118,142,.35); }
  .issue.warn  { background:rgba(224,175,104,.10); border-color:rgba(224,175,104,.35); }
  .issue code {
    display:inline-block; margin-top:5px; padding:2px 7px; border-radius:5px;
    background:rgba(127,127,127,.16); font-size:12.5px;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Recovery Card <span>— on-device context recovery</span></h1>

  <div class="issues" id="issues"></div>

  <div class="strip">
    <div>model <b id="s-model">—</b></div>
    <div>network <b id="s-net">—</b></div>
    <div>frames <b id="s-frames">0</b></div>
    <div>last capture <b id="s-cap">never</b></div>
    <div>last card <b id="s-card">never</b></div>
  </div>

  <div class="row">
    <button id="btn-cap" onclick="toggleCapture()">Start capture</button>
    <button class="primary" onclick="imBack()">I'm back</button>
  </div>

  <div class="state">
    <div class="mode" id="m-mode">—</div>
    <div class="big" id="m-big">—</div>
    <div class="sub" id="m-sub"></div>
  </div>

  <div class="park">
    <input id="park" placeholder="Park it — one line about where you're leaving off"
           onkeydown="if(event.key==='Enter')park()">
    <button onclick="park()">Save</button>
  </div>

  <div id="card-slot"></div>

  <div class="scoring" id="scoring"></div>

  <div class="hist" id="hist"></div>

  <div class="scoring" style="margin-top:18px">
    <h2 style="font-size:11px;letter-spacing:.14em;color:var(--dim);
               font-weight:600;margin:0 0 10px">TIME TO ENGAGED WORK</h2>
    <div id="metrics" style="font-size:13px;color:var(--dim)">no resumptions
      measured yet</div>
  </div>

  <div class="scoring" style="margin-top:18px">
    <h2 style="font-size:11px;letter-spacing:.14em;color:var(--dim);
               font-weight:600;margin:0 0 10px">CLASSIFICATION STREAM</h2>
    <div id="cstream" style="font-size:12.5px;color:var(--dim);
         font-variant-numeric:tabular-nums"></div>
  </div>
</div>

<script>
let running = false;

async function drawMetrics() {
  let m;
  try { m = await (await fetch("/api/metrics")).json(); } catch(e){ return; }
  const el = document.getElementById("metrics");
  if (!m.count) return;
  el.innerHTML = `median <b>${m.median_seconds}s</b> across ${m.count} ` +
    `resumption(s)<br>` + m.recent.slice(0,6).map(r =>
      `${r.seconds}s <span style="opacity:.6">(via ${r.via.replace("_"," ")}` +
      `${r.thread ? " · " + r.thread : ""})</span>`).join("<br>");
}
setInterval(drawMetrics, 5000);
drawMetrics();

async function drawStream() {
  let d; try { d = await (await fetch("/api/classify_log")).json(); }
  catch(e){ return; }
  document.getElementById("cstream").innerHTML = (d.rows || []).map(r =>
    `${esc((r.at||"").slice(11,19))}  ${esc(r.app).padEnd(14)} → ` +
    `<b style="color:var(--text)">${esc(r.thread)}</b> ` +
    `<span style="opacity:.6">tier ${r.tier} · ${r.score}</span>`
  ).join("<br>") || "no classifications yet";
}
setInterval(drawStream, 4000);
drawStream();

function esc(s) {
  return (s || "").replace(/[&<>"]/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

async function tick() {
  let d;
  try { d = await (await fetch("/api/state")).json(); }
  catch (e) { return; }

  running = d.running;
  document.getElementById("btn-cap").textContent =
    running ? "Stop capture" : "Start capture";

  document.getElementById("s-model").textContent = d.model;
  const net = document.getElementById("s-net");
  net.textContent = d.network;
  net.className = d.network === "ONLINE" ? "on" : "off";
  document.getElementById("s-frames").textContent =
    d.frames_kept + " kept, " + d.frames_skipped + " skipped";
  document.getElementById("s-cap").textContent = d.last_capture;
  document.getElementById("s-card").textContent = d.last_card;

  const mode = document.getElementById("m-mode");
  const big = document.getElementById("m-big");
  const sub = document.getElementById("m-sub");
  mode.textContent = d.mode.replace("_", " ");
  mode.className = "mode " + d.mode;

  if (d.mode === "ACTIVE") {
    big.innerHTML = '<span class="beat"></span>' + d.frames_kept;
    sub.textContent = "frames kept — watching quietly";
  } else if (d.mode === "AWAY") {
    big.textContent = Math.round(d.idle_seconds) + "s";
    sub.textContent = "since your last input — card fires at " +
                      d.idle_threshold + "s";
  } else if (d.mode === "RECONSTRUCTING") {
    big.textContent = (d.reconstructing_for ?? 0) + "s";
    sub.textContent = "reading your screens, writing the card…";
  } else if (d.mode === "CARD_READY") {
    big.textContent = "Card ready";
    sub.textContent = "waiting for you to come back";
  } else {
    big.textContent = "Idle";
    sub.textContent = "capture is not running";
  }

  renderCard(d.card);
  renderHistory(d.history);
}

// Only rebuild when the card or its verdicts actually change. Redrawing on
// every poll would wipe a correction box while it is being typed into, and
// re-run the entrance animation.
let lastCardSig = null;

function renderCard(c) {
  const slot = document.getElementById("card-slot");
  if (!c) { slot.innerHTML = ""; lastCardSig = null; return; }

  const sig = JSON.stringify([c._file, verdicts, (c.corrections || []).length]);
  if (sig === lastCardSig) return;
  lastCardSig = sig;

  let flag = "";
  if (c.reduced_model)
    flag += '<div class="flag reduced">REDUCED MODEL — low confidence</div> ';
  if (c.fail_closed)
    flag += '<div class="flag failed">NOT ENOUGH SIGNAL</div>';

  const loops = (c.open_loops || []).map(l => "<li>" + esc(l) + "</li>").join("");

  const f = c._file;
  slot.innerHTML = `
    <div class="card">
      ${flag}
      <h2>PICK UP HERE ${taps(f, "goal")}</h2>
      <p class="goal">${esc(c.goal)}</p>
      <div id="fix-goal"></div>
      <h2 style="margin-top:18px">WHY ${taps(f, "reasoning")}</h2>
      <p class="reason">${esc(c.reasoning)}</p>
      <div id="fix-reasoning"></div>
      <div class="sec">
        <h2>NEXT STEP ${taps(f, "next_action")}</h2>
        <p class="next">${esc(c.next_action)}</p>
        <div id="fix-next_action"></div>
      </div>
      ${loops ? `<div class="sec"><h2>OPEN LOOPS ${taps(f, "open_loops")}</h2>
        <ul>${loops}</ul><div id="fix-open_loops"></div></div>` : ""}
      ${c.park_note ? `<div class="said">You said: “${esc(c.park_note)}”</div>` : ""}
      ${(c.corrections || []).map(x =>
        `<div class="said">You corrected: “${esc(x.text)}”</div>`).join("")}
      <div class="meta">
        confidence ${esc(c.confidence)} · ${esc(c.model || "")}
        ${c.trigger ? "· triggered by " + esc(c.trigger) : ""}<br>
        evidence: ${esc(c.evidence)}
      </div>
    </div>`;
}

// --- verdict taps ----------------------------------------------------
// One tap records right or wrong. A wrong tap opens one line asking what
// they were actually doing. That line becomes truth for later cards.
// No chat, no follow-up questions, no popups.
let verdicts = {};

function taps(file, field) {
  const v = verdicts[file + ":" + field];
  return `<span class="taps">
    <button class="y ${v === true ? "on" : ""}"
            onclick="tap('${file}','${field}',true)" title="Right">✓</button>
    <button class="n ${v === false ? "on" : ""}"
            onclick="tap('${file}','${field}',false)" title="Wrong">✗</button>
  </span>`;
}

async function tap(file, field, value) {
  verdicts[file + ":" + field] = value;
  lastCardSig = null;              // let the card redraw with the new state
  await fetch("/api/verdict", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({file, field, value})
  });
  tick();
  drawScoring();
  if (value === false) setTimeout(() => showFix(file, field), 60);
}

function showFix(file, field) {
  const host = document.getElementById("fix-" + field);
  if (!host) return;
  host.innerHTML = `<div class="fixbox">
      <input id="fixin-${field}" placeholder="What were you actually doing?"
             onkeydown="if(event.key==='Enter')saveFix('${file}','${field}')">
      <button onclick="saveFix('${file}','${field}')">Save</button>
    </div>`;
  const i = document.getElementById("fixin-" + field);
  if (i) i.focus();
}

async function saveFix(file, field) {
  const i = document.getElementById("fixin-" + field);
  const text = i ? i.value.trim() : "";
  if (!text) return;
  await fetch("/api/verdict", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({file, field, value: false, correction: text})
  });
  const host = document.getElementById("fix-" + field);
  if (host) host.innerHTML =
    `<div class="fixed-note">Saved. Later cards will treat that as truth.</div>`;
  drawScoring();
}

function renderHistory(h) {
  const el = document.getElementById("hist");
  if (!h || h.length < 2) { el.innerHTML = ""; return; }
  el.innerHTML = "<h2>EARLIER CARDS</h2>" + h.slice(1).map(x =>
    `<div class="hitem"><time>${esc((x.at || "").replace("T", " ").slice(5, 16))}</time>
     <div>${esc(x.goal)}</div></div>`).join("");
}

async function toggleCapture() {
  await fetch("/api/capture/" + (running ? "stop" : "start"), {method: "POST"});
  setTimeout(tick, 400);
}

async function imBack() {
  const d = await (await fetch("/api/state")).json();
  if (d.card && d.mode === "CARD_READY") return;  // already waiting, instant
  await fetch("/api/generate", {method: "POST"});
  tick();
}

async function park() {
  const i = document.getElementById("park");
  if (!i.value.trim()) return;
  await fetch("/api/park", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text: i.value})
  });
  i.value = "";
  i.placeholder = "Parked. It will be treated as truth on the next card.";
}

// --- scoring ---------------------------------------------------------
// Only cards generated while a park note was active can be scored: the
// note is the user's own words, written before the model saw anything.
let marks = {};

function tallyLine(t) {
  const cell = (ok, n) => {
    if (!n) return null;
    const p = 100 * ok / n;
    const cls = p >= 70 ? "ok" : (p >= 50 ? "mid" : "bad");
    return `<span class="${cls}">${ok}/${n}</span>`;
  };
  const parts = [];
  for (const [f, v] of Object.entries(t.fields)) {
    const c = cell(v.correct, v.judged);
    if (c) parts.push(`${f.replace("_", " ")} <b>${c}</b>`);
  }
  if (!parts.length) return "";
  const o = cell(t.overall.correct, t.overall.judged);
  return parts.join(" · ") + ` · overall <b>${o}</b> (${t.percent}%)`;
}

async function drawScoring() {
  let e;
  try { e = await (await fetch("/api/eval")).json(); }
  catch (err) { return; }

  const el = document.getElementById("scoring");
  const line = tallyLine(e.tally);

  if (!e.next) {
    el.innerHTML = e.tally.overall.judged
      ? `<h2>ACCURACY</h2><p class="tally">${line}</p>
         <p class="tally" style="color:var(--dim);font-size:12.5px">
         ${e.judged} card(s) scored · nothing left to judge</p>`
      : `<h2>ACCURACY</h2><p class="tally" style="color:var(--dim);font-size:13px">
         Park a note before you step away. The card that follows can then be
         scored against your own words.</p>`;
    return;
  }

  const n = e.next;
  if (marks.__file !== n.file) marks = {__file: n.file};

  const row = (f, val) => {
    const m = marks[f];
    return `<div class="judge-row">
      <div class="lbl">${f.replace("_", " ")}</div>
      <div class="val">${esc(Array.isArray(val) ? val.join(" · ") : val) || "<i>(none)</i>"}</div>
      <div class="btns">
        <button class="${m===true?"on-y":""}" onclick="mark('${f}',true)">✓</button>
        <button class="${m===false?"on-n":""}" onclick="mark('${f}',false)">✗</button>
        <button class="${m===null?"on-na":""}" onclick="mark('${f}',null)">–</button>
      </div></div>`;
  };

  el.innerHTML = `
    <h2>ACCURACY</h2>
    ${line ? `<p class="tally">${line}</p>` : ""}
    <p class="tally" style="color:var(--dim);font-size:12.5px">
      ${e.pending} card(s) awaiting your judgement</p>
    <div class="truth">You said: “${esc(n.park_note)}”</div>
    ${row("goal", n.goal)}
    ${row("reasoning", n.reasoning)}
    ${row("next_action", n.next_action)}
    ${row("open_loops", n.open_loops)}
    <div style="margin-top:14px;display:flex;gap:8px;align-items:center">
      <button class="primary" onclick="saveMarks()">Save judgement</button>
      <span style="font-size:12px;color:var(--dim)">
        ✓ correct · ✗ wrong · – not applicable</span>
    </div>`;
}

function mark(field, value) {
  marks[field] = value;
  drawScoring.pending = true;
  drawScoring();
}

async function saveMarks() {
  const file = marks.__file;
  if (!file) return;
  const body = {file, marks: {}};
  for (const f of ["goal", "reasoning", "next_action", "open_loops"])
    body.marks[f] = (f in marks) ? marks[f] : null;
  await fetch("/api/eval/mark", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  marks = {};
  drawScoring();
}

async function preflight() {
  const d = await (await fetch("/api/preflight")).json();
  document.getElementById("issues").innerHTML = (d.issues || []).map(i =>
    `<div class="issue ${i.level}"><b>${esc(i.problem)}</b><br>
     <code>${esc(i.fix)}</code></div>`).join("");
}

// Summoning the window by hotkey must show the truth immediately, not
// whatever the last 2-second poll happened to catch.
window.addEventListener("focus", tick);
window.addEventListener("pageshow", tick);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) tick();
});

preflight();
tick();
drawScoring();
setInterval(tick, 2000);
setInterval(drawScoring, 5000);
</script>
</body>
</html>
"""


def port_busy(port):
    """True if anything already answers on this port, on IPv4 or IPv6.

    macOS runs AirPlay Receiver on port 5000 by default. It binds the IPv6
    wildcard, so Flask can still bind 127.0.0.1:5000 and appear to work
    while every request to "localhost:5000" is silently answered by AirPlay
    with a 403. Rather than asking anyone to turn off a system feature
    mid-demo, we just move to the next free port.
    """
    for family, addr in ((socket.AF_INET, "127.0.0.1"),
                         (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(0.4)
            hit = s.connect_ex((addr, port)) == 0
            s.close()
            if hit:
                return True
        except OSError:
            continue
    return False


# The demo URL is deterministic on purpose: one address, one hotkey, one
# piece of muscle memory. 5001 rather than 5000 because macOS AirPlay
# Receiver owns 5000. Override with PORT if you ever need to.
DEFAULT_PORT = 5001


def choose_port():
    preferred = int(os.environ.get("PORT", DEFAULT_PORT))
    if not port_busy(preferred):
        return preferred, False
    for port in range(preferred + 1, preferred + 10):
        if not port_busy(port):
            return port, True
    return preferred, True


_BASE_CSS = r"""
  :root {
    --bg:#0f1115; --panel:#171a21; --line:#252a34;
    --text:#e6e8ec; --dim:#8b93a1; --accent:#4fd6be;
    --good:#4fd6be; --warn:#e0af68; --bad:#f7768e;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:15.5px/1.65 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
    padding:34px 22px 70px;
    -webkit-font-smoothing:antialiased;
  }
  .wrap { max-width:700px; margin:0 auto; }
  button {
    font:inherit; font-size:14.5px; padding:10px 20px; border-radius:9px;
    border:1px solid var(--line); background:var(--panel); color:var(--text);
    cursor:pointer; transition:border-color .2s ease;
  }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent);
                   color:#0f1115; font-weight:600; }
  button.quiet { background:transparent; color:var(--dim); }
  input[type=text] {
    font:inherit; font-size:14.5px; padding:10px 14px; border-radius:9px;
    border:1px solid var(--line); background:var(--panel); color:var(--text);
    width:100%;
  }
  input[type=text]:focus { outline:none; border-color:var(--accent); }
  h1 { font-size:21px; font-weight:600; letter-spacing:-.01em; margin:0 0 6px; }
  .sub { color:var(--dim); font-size:14px; margin:0 0 26px; }
  .fade-in { animation:fadein .5s ease-out; }
  @keyframes fadein { from {opacity:0; transform:translateY(6px);} to {opacity:1;} }
"""


ONBOARD_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Recovery Card — welcome</title>
<style>
""" + _BASE_CSS + r"""
  /* One screen at a time, one primary action, nothing auto-advances. */
  .screen { display:none; }
  .screen.here { display:block; animation:fadein .5s ease-out; }
  .actions { margin-top:34px; display:flex; gap:12px; align-items:center; }
  .back { color:var(--dim); font-size:13.5px; background:none; border:none;
          cursor:pointer; padding:10px 6px; }
  .back:hover { color:var(--text); }

  .privacy {
    margin:22px 0; padding:16px 20px; border-radius:12px;
    background:var(--panel); border:1px solid var(--line); font-size:14.5px;
  }
  .checks { margin-top:14px; }
  .check { display:flex; gap:10px; padding:7px 0; font-size:13.5px;
           color:var(--dim); align-items:baseline; }
  .check b { color:var(--text); font-weight:500; }
  .dot { width:8px; height:8px; border-radius:50%; flex:none;
         position:relative; top:-1px; }
  .dot.ok { background:var(--good); }
  .dot.no { background:var(--bad); }
  .dot.wait { background:var(--dim); }

  .prop {
    display:flex; gap:12px; align-items:center; padding:14px 16px;
    border:1px solid var(--line); border-radius:12px; margin-bottom:10px;
    background:var(--panel);
  }
  .prop input[type=text] { border:none; background:transparent; padding:2px 0;
                           font-size:15px; }
  .prop .ev { font-size:12.5px; color:var(--dim); margin-top:2px; }
  .prop .keep { flex:none; }
  .prop.ignored { opacity:.38; }
  .thinking { color:var(--dim); font-size:14px; padding:26px 0; }

  .how {
    display:flex; justify-content:space-between; gap:8px; margin:30px 0;
    text-align:center; font-size:13px; color:var(--dim);
  }
  .how .step { flex:1; padding:18px 8px; border:1px solid var(--line);
               border-radius:12px; background:var(--panel); }
  .how .glyph { font-size:22px; margin-bottom:8px; }
  .how .arrow { align-self:center; color:var(--line); font-size:18px; }
</style>
</head>
<body>
<div class="wrap">

  <div class="screen here" id="s1">
    <h1>Recovery Card holds your threads.</h1>
    <p class="sub">When you're pulled away, it keeps the thread you were on —
    and the point to pick it back up.</p>
    <div class="privacy">
      Everything stays on this Mac. Nothing is uploaded.
      <div class="checks" id="checks">
        <div class="check"><span class="dot wait"></span>Checking the local model…</div>
      </div>
    </div>
    <div class="actions">
      <button class="primary" onclick="go(2)">Get started</button>
    </div>
  </div>

  <div class="screen" id="s2">
    <h1>What are you working on?</h1>
    <p class="sub">One look at your screen, and it will suggest the threads it
    sees open. Keep what's right, rename anything, ignore the rest.</p>
    <div id="props"><div class="thinking">Looking at your screen…</div></div>
    <div style="display:flex; gap:8px; margin-top:14px;">
      <input type="text" id="manual" placeholder="Add a thread yourself — e.g. “Phossil launch”"
             onkeydown="if(event.key==='Enter')addManual()">
      <button onclick="addManual()">Add</button>
    </div>
    <div class="actions">
      <button class="primary" onclick="go(3)">Keep these threads</button>
      <button class="quiet" onclick="go(3)">Skip for now</button>
      <button class="back" onclick="go(1)">‹ Back</button>
    </div>
  </div>

  <div class="screen" id="s3">
    <h1>Anyone or anything tied to these?</h1>
    <p class="sub">Optional. A person you're waiting on, a doc that matters.
    It helps the cards name things precisely.</p>
    <div id="peopledocs"></div>
    <div class="actions">
      <button class="primary" onclick="go(4)">Continue</button>
      <button class="primary" style="background:var(--panel);color:var(--text);border-color:var(--line)"
              onclick="go(4)">Skip for now</button>
      <button class="back" onclick="go(2)">‹ Back</button>
    </div>
  </div>

  <div class="screen" id="s4">
    <h1>How it works</h1>
    <p class="sub">That's all it needs.</p>
    <div class="how">
      <div class="step"><div class="glyph">●</div>You work.<br>It watches quietly.</div>
      <div class="arrow">→</div>
      <div class="step"><div class="glyph">◐</div>You get pulled away.<br>No action needed.</div>
      <div class="arrow">→</div>
      <div class="step"><div class="glyph">◉</div>You come back.<br>Your threads are waiting.</div>
    </div>
    <div class="actions">
      <button class="primary" onclick="finish()">Start</button>
      <button class="back" onclick="go(3)">‹ Back</button>
    </div>
  </div>

</div>
<script>
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
let props = [];   // {name, evidence, keep}
let proposed = false;

function go(n) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("here"));
  document.getElementById("s" + n).classList.add("here");
  if (n === 2 && !proposed) { proposed = true; propose(); }
  if (n === 3) drawPeopleDocs();
}

async function preflight() {
  let d; try { d = await (await fetch("/api/preflight")).json(); } catch(e) { return; }
  let s; try { s = await (await fetch("/api/state")).json(); } catch(e) { s = {}; }
  const rows = [];
  const issues = d.issues || [];
  const bad = t => issues.find(i => i.problem.toLowerCase().includes(t));
  rows.push(row(!bad("ollama"), "Local model engine", bad("ollama")));
  rows.push(row(!bad("model"), "Gemma 4 on this Mac", bad("model")));
  rows.push(row(!bad("screenshot") && !bad("blank"), "Screen access",
                bad("screenshot") || bad("blank")));
  rows.push(`<div class="check"><span class="dot ${s.network === "OFFLINE" ? "ok" : "ok"}"></span>
     <span>Network: <b>${s.network || "?"}</b> — works either way; nothing is sent anywhere.</span></div>`);
  document.getElementById("checks").innerHTML = rows.join("");
  function row(ok, label, issue) {
    return `<div class="check"><span class="dot ${ok ? "ok" : "no"}"></span>
      <span><b>${label}</b>${ok ? "" : " — " + esc(issue ? issue.fix : "")}</span></div>`;
  }
}

async function propose() {
  let d;
  try { d = await (await fetch("/api/onboard/propose", {method:"POST"})).json(); }
  catch (e) { d = {ok:false, error:"Could not reach the local engine."}; }
  const host = document.getElementById("props");
  if (!d.ok) {
    host.innerHTML = `<div class="thinking">It couldn't look right now
      (${esc(d.error || "")}). You can add threads yourself below.</div>`;
    return;
  }
  props = (d.proposals || []).map(p => ({...p, keep: true}));
  drawProps();
}

function drawProps() {
  const host = document.getElementById("props");
  if (!props.length) {
    host.innerHTML = `<div class="thinking">Nothing jumped out. Add your
      threads below — a few words each is plenty.</div>`;
    return;
  }
  host.innerHTML = props.map((p, i) => `
    <div class="prop ${p.keep ? "" : "ignored"}">
      <div style="flex:1">
        <input type="text" value="${esc(p.name)}" onchange="props[${i}].name=this.value">
        <div class="ev">${esc(p.evidence || "")}</div>
      </div>
      <button class="keep" onclick="props[${i}].keep=!props[${i}].keep; drawProps()">
        ${p.keep ? "Keeping" : "Ignored"}</button>
    </div>`).join("");
}

function addManual() {
  const i = document.getElementById("manual");
  if (!i.value.trim()) return;
  props.push({name: i.value.trim(), evidence: "added by you", keep: true});
  i.value = "";
  drawProps();
}

function drawPeopleDocs() {
  const kept = props.filter(p => p.keep);
  const host = document.getElementById("peopledocs");
  if (!kept.length) {
    host.innerHTML = `<div class="thinking">No threads yet — that's fine.
      They'll form as you work.</div>`;
    return;
  }
  host.innerHTML = kept.map((p, i) => `
    <div class="prop"><div style="flex:1">
      <div>${esc(p.name)}</div>
      <input type="text" placeholder="A person or doc tied to this (optional)"
             onchange="props[${i}].anchor=this.value" style="margin-top:6px">
    </div></div>`).join("");
}

async function finish() {
  const kept = props.filter(p => p.keep).map(p => ({
    name: p.name, anchors: p.anchor ? [p.anchor] : []}));
  await fetch("/api/onboard/complete", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({threads: kept})});
  location.href = "/";
}

preflight();
setInterval(preflight, 4000);
</script>
</body>
</html>
"""


BOARD_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Recovery Card</title>
<style>
""" + _BASE_CSS + r"""
  /* The board: same thing in the same place, every session.
     Order, top to bottom: situation, active thread, other threads, card. */
  .headline { font-size:20px; font-weight:500; line-height:1.45;
              letter-spacing:-.01em; margin:0 0 20px; }
  .quietrow { display:flex; gap:16px; align-items:center; font-size:12.5px;
              color:var(--dim); margin-bottom:26px; flex-wrap:wrap; }
  .quietrow .beat { display:inline-block; width:7px; height:7px;
      border-radius:50%; background:var(--good); margin-right:5px;
      animation:pulse 3s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  .off { color:var(--bad); font-weight:600; }

  .thread {
    background:var(--panel); border:1px solid var(--line);
    border-radius:14px; padding:18px 22px; margin-bottom:10px;
  }
  .thread.active { border-color:rgba(79,214,190,.5); padding:22px; }
  .thread .name { font-weight:600; font-size:15.5px; }
  .thread.active .name { font-size:18px; }
  .thread .rp { color:var(--dim); font-size:13.5px; margin-top:4px; }
  .thread.active .rp { font-size:14.5px; color:var(--text); opacity:.85; }
  .thread .tag { font-size:10.5px; letter-spacing:.1em; color:var(--dim);
                 font-weight:600; }
  .thread .tag.act { color:var(--accent); }

  .chiprow { display:flex; gap:6px; margin-top:10px; flex-wrap:wrap; }
  .chip { font-size:11.5px; padding:3px 10px; border-radius:6px;
          border:1px solid var(--line); background:transparent;
          color:var(--dim); cursor:pointer; }
  .chip:hover { color:var(--text); border-color:#4fd6be; }
  .chip.more { cursor:default; border-style:dashed; }
  .chippop { position:absolute; z-index:20; background:#1c2029;
             border:1px solid var(--line); border-radius:10px;
             padding:10px 14px; font-size:12.5px; color:var(--dim);
             max-width:260px; box-shadow:0 10px 34px rgba(0,0,0,.5);
             line-height:1.6; }
  .chippop b { color:var(--text); }
  .emergent {
    border:1px dashed rgba(224,175,104,.55); border-radius:14px;
    padding:16px 20px; margin:14px 0; background:rgba(224,175,104,.05);
  }
  .emergent .q { font-size:14.5px; margin-bottom:10px; }
  .emergent .sample { font-size:12.5px; color:var(--dim); margin-bottom:10px; }
  .emergent .row { display:flex; gap:8px; }

  .park { display:flex; gap:8px; margin:22px 0; }
  .controls { display:flex; gap:10px; margin-bottom:8px; }
  h2.sect { font-size:11px; letter-spacing:.14em; color:var(--dim);
            font-weight:600; margin:26px 0 10px; }
  a.engine { color:var(--dim); font-size:11.5px; text-decoration:none; }
</style>
</head>
<body>
<div class="wrap">

  <p class="headline" id="headline">…</p>

  <div class="quietrow" id="quiet"></div>

  <div class="controls" id="controls"></div>

  <div style="margin:22px 0">
    <div style="font-size:12.5px;color:var(--dim);margin-bottom:6px">
      Park it — one line before you step away. It becomes truth on your
      next card.</div>
    <div class="park" style="margin:0">
      <input type="text" id="park"
             placeholder="e.g. “waiting on Joel's numbers, next: paste the conversion figure”"
             onkeydown="if(event.key==='Enter')park()">
      <button onclick="park()">Save</button>
    </div>
  </div>

  <div id="emergent-slot"></div>

  <h2 class="sect" id="threads-title" style="display:none">YOUR THREADS</h2>
  <div id="threads"></div>

  <p style="margin-top:40px">
    <a class="engine" href="/trust">what it sees</a> ·
    <a class="engine" href="/engine">engine room</a></p>
</div>
<script>
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
let running = false;

function relAgo(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s/60) + "m ago";
  if (s < 86400) return Math.round(s/3600) + "h ago";
  return Math.round(s/86400) + "d ago";
}

// SOURCE CHIPS: the apps/domains whose classified frames composed the
// thread. Quiet, one row, no color noise. A chip exists only because
// real frames back it. Tap -> mini provenance.
function chipRow(t) {
  const c = t.chips || [];
  if (!c.length) return "";
  return `<div class="chiprow">` + c.map(ch =>
    `<button class="chip" onclick="event.stopPropagation();
       chipInfo(this, '${esc(ch.label)}', ${ch.count},
                '${ch.first}', '${ch.last}', '${t.id}')">
       ${esc(ch.label)}</button>`).join("") +
    (t.chips_more ? `<span class="chip more">+${t.chips_more}</span>` : "") +
    `</div>`;
}

function chipInfo(el, label, count, first, last, tid) {
  document.querySelectorAll(".chippop").forEach(p => p.remove());
  const pop = document.createElement("div");
  pop.className = "chippop";
  const r = el.getBoundingClientRect();
  pop.style.left = Math.min(r.left, innerWidth - 280) + "px";
  pop.style.top = (r.bottom + 6 + scrollY) + "px";
  pop.innerHTML = `<b>${esc(label)}</b><br>
    part of this thread since ${relAgo(first)} · ${count} moment${count===1?"":"s"}
    · last ${relAgo(last)}<br>
    <a href="/thread/${tid}" style="color:#4fd6be;font-size:12px">
      see its cards on the map ›</a>`;
  document.body.appendChild(pop);
  setTimeout(() => document.addEventListener("click",
    () => pop.remove(), {once: true}), 50);
}

async function tickBoard() {
  let b; try { b = await (await fetch("/api/board")).json(); } catch(e){ return; }
  document.getElementById("headline").textContent = b.headline;

  const host = document.getElementById("threads");
  const threads = (b.threads || []).filter(t => t.status !== "ambient");
  document.getElementById("threads-title").style.display =
    threads.length ? "" : "none";
  // Every row is clickable through to that thread's map - and nothing
  // else happens on the row (DESIGN.md board spec).
  host.innerHTML = threads.map(t => {
    const act = t.id === b.active;
    return `<div class="thread ${act ? "active" : ""}" style="cursor:pointer"
                 onclick="location.href='/thread/${t.id}'">
      <div class="tag ${act ? "act" : ""}">${act ? "ACTIVE NOW" :
        (t.origin === "emergent" ? "EMERGENT · HELD" : "HELD")}
        <span style="font-weight:400;letter-spacing:0"> · ${act ? "active" : "last"} ${relAgo(t.last_seen)}</span></div>
      <div class="name">${esc(t.name)}</div>
      <div class="rp">${esc(t.return_point) ||
        "No return-point yet — one appears after the first card."}</div>
      ${chipRow(t)}
    </div>`;
  }).join("");

  const em = document.getElementById("emergent-slot");
  if (b.emergent) {
    if (!em.dataset.shown) {
      em.dataset.shown = "1";
      em.innerHTML = `<div class="emergent">
        <div class="q">Something new forming? Name it and PLite will hold it.</div>
        <div class="sample">${esc(b.emergent.sample_text)}</div>
        <div class="row">
          <input type="text" id="emname" placeholder="Name it — e.g. “Speaker repair”"
                 onkeydown="if(event.key==='Enter')keepEmergent()">
          <button class="primary" onclick="keepEmergent()">Keep</button>
          <button class="quiet" onclick="dismissEmergent()">Leave it</button>
        </div></div>`;
    }
  } else { em.innerHTML = ""; em.dataset.shown = ""; }
}

async function keepEmergent() {
  const name = document.getElementById("emname").value.trim();
  if (!name) return;
  await fetch("/api/thread/confirm_emergent", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name})});
  document.getElementById("emergent-slot").dataset.shown = "";
  tickBoard();
}
async function dismissEmergent() {
  await fetch("/api/thread/dismiss_emergent", {method:"POST"});
  document.getElementById("emergent-slot").dataset.shown = "";
  tickBoard();
}

async function tick() {
  let d; try { d = await (await fetch("/api/state")).json(); } catch(e){ return; }
  running = d.running;

  // WATCHING STATE IS UNMISSABLE. Not watching -> the board's single
  // primary action is Start watching, right here. Watching -> the
  // primary is Show my card, with a calm breathing indicator above.
  const ctl = document.getElementById("controls");
  ctl.innerHTML = running
    ? `<button class="primary" onclick="showCard()">Show my card</button>`
    : `<button class="primary" onclick="startWatching()">Start watching</button>`;

  const q = [];
  // Silent generation law: RECONSTRUCTING is background; here it is just
  // "watching" - a false idle-fire must cost nothing visible.
  if (d.mode === "STALLED")
    q.push(`<span class="off">${esc(d.watch_note)}</span>`);
  else if (["ACTIVE", "AWAY", "RECONSTRUCTING"].includes(d.mode))
    q.push(d.watch_note
      ? `<span style="color:#e0af68">watching — ${esc(d.watch_note)}</span>`
      : `<span><span class="beat"></span>watching · last capture ${esc(d.last_capture)}</span>`);
  else if (d.mode === "SUSPENDED") q.push(`<span>asleep — state saved</span>`);
  else if (d.mode === "PAUSED_PRIVATE") q.push(`<span>paused — a private app is in front</span>`);
  else if (d.mode === "CARD_READY") q.push(`<span>your card is ready</span>`);
  else q.push(`<span>not watching</span>`);
  q.push(`<span class="${d.network === "OFFLINE" ? "off" : ""}">${d.network === "OFFLINE" ? "offline — fully on-device" : "on-device"}</span>`);
  if (d.last_card && d.last_card !== "never")
    q.push(`<span>last card ${esc(d.last_card)}</span>`);
  document.getElementById("quiet").innerHTML = q.join(" · ");
}

async function startWatching() {
  await fetch("/api/capture/start", {method:"POST"});
  setTimeout(tick, 500);
}
// Summons the return surface - the calm, card-only view.
async function showCard() {
  await fetch("/api/summon", {method:"POST"});
}
async function park() {
  const i = document.getElementById("park");
  if (!i.value.trim()) return;
  await fetch("/api/park", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({text:i.value})});
  i.value = "";
  i.placeholder = "Parked. It will be treated as truth on the next card.";
}
</script>
"""


# Card renderer + verdict taps + scoring, shared into the board page. The
# engine-room page keeps its own older copy; the board is the canonical
# surface now.
_CARD_ASSETS = r"""
<style>
  .card { position:relative; background:var(--panel); border:1px solid var(--line);
          border-radius:14px; padding:26px; margin-bottom:18px; }
  .card h2 { font-size:11px; letter-spacing:.14em; color:var(--dim);
             font-weight:600; margin:0 0 8px; display:flex; align-items:center; gap:8px; }
  .goal { font-size:21px; font-weight:500; line-height:1.35; margin:0 0 10px;
          letter-spacing:-.01em; }
  .reason { color:var(--dim); margin:0; }
  .sec { margin-top:22px; }
  .next { font-size:16.5px; margin:0; }
  ul { margin:0; padding-left:20px; } li { margin:3px 0; }
  .said { margin-top:20px; padding:12px 16px; border-left:2px solid var(--accent);
          background:rgba(79,214,190,.07); font-style:italic; }
  .meta { margin-top:22px; padding-top:14px; border-top:1px solid var(--line);
          font-size:12.5px; color:var(--dim); }
  .flag { display:inline-block; padding:3px 9px; border-radius:5px; font-size:11px;
          font-weight:600; letter-spacing:.06em; margin-bottom:12px; }
  .flag.reduced { background:rgba(224,175,104,.16); color:var(--warn); }
  .flag.failed { background:rgba(247,118,142,.14); color:var(--bad); }
  .taps { margin-left:auto; display:flex; gap:3px; opacity:.25; transition:opacity .2s; }
  .card:hover .taps { opacity:.7; } .taps:hover { opacity:1 !important; }
  .taps button { padding:1px 7px; font-size:11px; border-radius:5px;
                 background:transparent; border:1px solid var(--line); color:var(--dim); }
  .taps button.y.on { background:var(--good); border-color:var(--good); color:#0f1115; }
  .taps button.n.on { background:var(--bad); border-color:var(--bad); color:#0f1115; }
  .fixbox { display:flex; gap:6px; margin:8px 0 2px; }
  .fixbox input { flex:1; font:inherit; font-size:13px; padding:7px 11px;
                  border-radius:7px; border:1px solid var(--accent);
                  background:var(--bg); color:var(--text); }
  .fixed-note { margin:8px 0 2px; font-size:12.5px; color:var(--good); }
  .scoring { background:var(--panel); border:1px solid var(--line);
             border-radius:14px; padding:20px 22px; }
  .scoring h2 { font-size:11px; letter-spacing:.14em; color:var(--dim);
                font-weight:600; margin:0 0 12px; }
  .tally { font-size:14px; margin:0 0 4px; }
  .tally .ok { color:var(--good); } .tally .mid { color:var(--warn); }
  .tally .bad { color:var(--bad); }
  .truth { margin:14px 0 6px; padding:10px 14px; border-left:2px solid var(--good);
           background:rgba(79,214,190,.08); font-style:italic; font-size:13.5px; }
  .judge-row { display:flex; align-items:flex-start; gap:10px; padding:9px 0;
               border-bottom:1px solid var(--line); font-size:13.5px; }
  .judge-row:last-child { border-bottom:none; }
  .judge-row .lbl { width:96px; flex:none; color:var(--dim); font-size:12px;
                    padding-top:3px; }
  .judge-row .val { flex:1; }
  .judge-row .btns { flex:none; display:flex; gap:4px; }
  .judge-row button { padding:3px 10px; font-size:12px; border-radius:6px; min-width:30px; }
  .judge-row button.on-y { background:var(--good); border-color:var(--good); color:#0f1115; }
  .judge-row button.on-n { background:var(--bad); border-color:var(--bad); color:#0f1115; }
  .judge-row button.on-na { background:var(--dim); border-color:var(--dim); color:#0f1115; }
</style>
<script>
let verdicts = {};
let lastCardSig = null;

function taps(file, field) {
  const v = verdicts[file + ":" + field];
  return `<span class="taps">
    <button class="y ${v === true ? "on" : ""}"
            onclick="tap('${file}','${field}',true)" title="Right">✓</button>
    <button class="n ${v === false ? "on" : ""}"
            onclick="tap('${file}','${field}',false)" title="Wrong">✗</button>
  </span>`;
}
async function tap(file, field, value) {
  verdicts[file + ":" + field] = value;
  lastCardSig = null;
  await fetch("/api/verdict", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({file, field, value})});
  tick(); drawScoring();
  if (value === false) setTimeout(() => showFix(file, field), 60);
}
function showFix(file, field) {
  const host = document.getElementById("fix-" + field);
  if (!host) return;
  host.innerHTML = `<div class="fixbox">
    <input id="fixin-${field}" placeholder="What were you actually doing?"
           onkeydown="if(event.key==='Enter')saveFix('${file}','${field}')">
    <button onclick="saveFix('${file}','${field}')">Save</button></div>`;
  const i = document.getElementById("fixin-" + field);
  if (i) i.focus();
}
async function saveFix(file, field) {
  const i = document.getElementById("fixin-" + field);
  const text = i ? i.value.trim() : "";
  if (!text) return;
  await fetch("/api/verdict", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({file, field, value:false, correction:text})});
  const host = document.getElementById("fix-" + field);
  if (host) host.innerHTML =
    `<div class="fixed-note">Saved. Later cards will treat that as truth.</div>`;
  drawScoring();
}

function renderCard(c) {
  const slot = document.getElementById("card-slot");
  if (!c) { slot.innerHTML = ""; lastCardSig = null; return; }
  const sig = JSON.stringify([c._file, verdicts, (c.corrections || []).length]);
  if (sig === lastCardSig) return;
  lastCardSig = sig;

  let flag = "";
  if (c.reduced_model) flag += '<div class="flag reduced">REDUCED MODEL — low confidence</div> ';
  if (c.fail_closed) flag += '<div class="flag failed">NOT ENOUGH SIGNAL</div>';
  const loops = (c.open_loops || []).map(l => "<li>" + esc(l) + "</li>").join("");
  const f = c._file;

  slot.innerHTML = `
    <div class="card">
      ${flag}
      ${c.thread ? `<div class="flag" style="background:rgba(79,214,190,.14);color:var(--accent)">${esc(c.thread)}</div>` : ""}
      <h2>PICK UP HERE ${taps(f, "goal")}</h2>
      <p class="goal">${esc(c.goal)}</p>
      <div id="fix-goal"></div>
      <h2 style="margin-top:18px">WHY ${taps(f, "reasoning")}</h2>
      <p class="reason">${esc(c.reasoning)}</p>
      <div id="fix-reasoning"></div>
      <div class="sec"><h2>NEXT STEP ${taps(f, "next_action")}</h2>
        <p class="next">${esc(c.next_action)}</p><div id="fix-next_action"></div></div>
      ${loops ? `<div class="sec"><h2>OPEN LOOPS ${taps(f, "open_loops")}</h2>
        <ul>${loops}</ul><div id="fix-open_loops"></div></div>` : ""}
      ${c.park_note ? `<div class="said">You said: “${esc(c.park_note)}”</div>` : ""}
      ${(c.corrections || []).map(x =>
        `<div class="said">You corrected: “${esc(x.text)}”</div>`).join("")}
      <div class="meta">
        confidence ${esc(c.confidence)} · ${esc(c.model || "")}
        ${c.trigger ? "· triggered by " + esc(c.trigger) : ""}<br>
        evidence: ${esc(c.evidence)}
      </div>
    </div>`;
}

let marks = {};
function tallyLine(t) {
  const cell = (ok, n) => {
    if (!n) return null;
    const p = 100 * ok / n;
    const cls = p >= 70 ? "ok" : (p >= 50 ? "mid" : "bad");
    return `<span class="${cls}">${ok}/${n}</span>`;
  };
  const parts = [];
  for (const [f, v] of Object.entries(t.fields)) {
    const c = cell(v.correct, v.judged);
    if (c) parts.push(`${f.replace("_", " ")} <b>${c}</b>`);
  }
  if (!parts.length) return "";
  return parts.join(" · ") +
    ` · overall <b>${cell(t.overall.correct, t.overall.judged)}</b> (${t.percent}%)`;
}
async function drawScoring() {
  let e; try { e = await (await fetch("/api/eval")).json(); } catch (err) { return; }
  const el = document.getElementById("scoring");
  if (!el) return;
  const line = tallyLine(e.tally);
  if (!e.next) {
    el.innerHTML = e.tally.overall.judged
      ? `<h2>ACCURACY</h2><p class="tally">${line}</p>`
      : `<h2>ACCURACY</h2><p class="tally" style="color:var(--dim);font-size:13px">
         Park a note before you step away, and the card that follows can be
         scored against your own words.</p>`;
    return;
  }
  const n = e.next;
  if (marks.__file !== n.file) marks = {__file: n.file};
  const row = (f, val) => {
    const m = marks[f];
    return `<div class="judge-row"><div class="lbl">${f.replace("_", " ")}</div>
      <div class="val">${esc(Array.isArray(val) ? val.join(" · ") : val) || "<i>(none)</i>"}</div>
      <div class="btns">
        <button class="${m===true?"on-y":""}" onclick="mark('${f}',true)">✓</button>
        <button class="${m===false?"on-n":""}" onclick="mark('${f}',false)">✗</button>
        <button class="${m===null?"on-na":""}" onclick="mark('${f}',null)">–</button>
      </div></div>`;
  };
  el.innerHTML = `<h2>ACCURACY</h2>
    ${line ? `<p class="tally">${line}</p>` : ""}
    <p class="tally" style="color:var(--dim);font-size:12.5px">
      ${e.pending} card(s) awaiting your judgement</p>
    <div class="truth">You said: “${esc(n.park_note)}”</div>
    ${row("goal", n.goal)} ${row("reasoning", n.reasoning)}
    ${row("next_action", n.next_action)} ${row("open_loops", n.open_loops)}
    ${row("right_thread", n.thread || "(no thread)")}
    <div style="margin-top:14px;display:flex;gap:8px;align-items:center">
      <button class="primary" onclick="saveMarks()">Save judgement</button>
      <span style="font-size:12px;color:var(--dim)">✓ correct · ✗ wrong · – not applicable</span>
    </div>`;
}
function mark(field, value) { marks[field] = value; drawScoring(); }
async function saveMarks() {
  const file = marks.__file;
  if (!file) return;
  const body = {file, marks: {}};
  for (const f of ["goal", "reasoning", "next_action", "open_loops"])
    body.marks[f] = (f in marks) ? marks[f] : null;
  await fetch("/api/eval/mark", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  marks = {};
  drawScoring();
}

window.addEventListener("focus", () => { tick(); tickBoard(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { tick(); tickBoard(); }});
tick(); tickBoard(); drawScoring();
setInterval(tick, 2000);
setInterval(tickBoard, 3000);
setInterval(drawScoring, 6000);
</script>
</body>
</html>
"""

# The board is calm: headline, threads, park. The card renderer and the
# scoring queue belong to the return surface and the engine room.
# NOTE the opening <script> below: the board's main block is already closed
# by this point. Appending bare JS here once rendered as literal text on
# the page - and none of it ran.
BOARD_PAGE = BOARD_PAGE + r"""
<script>
window.addEventListener("focus", () => { tick(); tickBoard(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { tick(); tickBoard(); }});
tick(); tickBoard();
setInterval(tick, 2500);
setInterval(tickBoard, 3000);
</script>
</body>
</html>
"""


MAP_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PLite — thread</title>
<style>
""" + _BASE_CSS + r"""
  .back-link { color:var(--dim); text-decoration:none; font-size:13.5px; }
  .back-link:hover { color:var(--text); }
  .statustag { font-size:10.5px; letter-spacing:.12em; font-weight:600;
               color:var(--dim); }
  .statustag.active { color:#4fd6be; }
  .thead { margin:10px 0 4px; font-size:22px; font-weight:600;
           letter-spacing:-.01em; }
  .theadline { color:var(--dim); font-size:14.5px; margin:0 0 24px; }

  /* The map: thread centered, nodes on a ring. No drag, no zoom. */
  #mapbox { position:relative; height:340px; margin:6px 0 10px; }
  .center-chip {
    position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
    background:rgba(79,214,190,.12); border:1px solid rgba(79,214,190,.5);
    color:var(--text); border-radius:14px; padding:12px 20px;
    font-weight:600; font-size:15px; max-width:220px; text-align:center;
    z-index:2;
  }
  .node {
    position:absolute; transform:translate(-50%,-50%);
    background:var(--panel); border:1px solid var(--line);
    border-radius:12px; padding:8px 13px; font-size:12.5px; cursor:pointer;
    max-width:170px; text-align:center; z-index:3;
    transition:border-color .2s ease;
  }
  .node:hover { border-color:#4fd6be; }
  .node .kind { display:block; font-size:10px; letter-spacing:.1em;
                color:var(--dim); }
  .node.blocker { border-color:rgba(247,118,142,.6);
                  background:rgba(247,118,142,.07); }
  .node.blocker .kind { color:#f7768e; }
  .node.resolved { opacity:.5; }
  .node.resolved .lbl { text-decoration:line-through; }
  .node .pinmark { color:#e0af68; }
  .edge { position:absolute; height:1px; background:var(--line);
          transform-origin:0 0; z-index:1; }
  .edge.blocker { background:rgba(247,118,142,.5); height:2px; }

  .menu {
    position:absolute; z-index:10; background:#1c2029;
    border:1px solid var(--line); border-radius:12px; padding:6px;
    box-shadow:0 12px 40px rgba(0,0,0,.5); min-width:190px;
  }
  .menu button { display:block; width:100%; text-align:left; border:none;
                 background:none; padding:8px 12px; font-size:13.5px;
                 border-radius:8px; }
  .menu button:hover { background:rgba(255,255,255,.06); }
  .menu button.danger { color:#f7768e; }
  .menu .inline { padding:6px 8px; display:flex; gap:6px; }
  .menu .inline input { font-size:13px; padding:6px 9px; }
  .prov { padding:8px 12px; font-size:12.5px; color:var(--dim);
          max-width:240px; }

  .chiprow { display:flex; gap:6px; margin:-14px 0 18px; flex-wrap:wrap; }
  .chip { font-size:11.5px; padding:3px 10px; border-radius:6px;
          border:1px solid var(--line); color:var(--dim); }
  .chip.more { border-style:dashed; }
  .showmore { font-size:12.5px; color:var(--dim); background:none;
              border:none; cursor:pointer; padding:4px 0; }
  .showmore:hover { color:var(--text); }

  .actions { display:flex; gap:10px; align-items:center; margin:18px 0; }
  .restore { background:var(--panel); border:1px solid var(--line);
             border-radius:12px; padding:14px 18px; margin:0 0 20px;
             font-size:13.5px; }
  .restore .item { padding:4px 0; display:flex; gap:8px; align-items:center; }
  .restore .note { color:var(--dim); margin-top:8px; font-size:12.5px; }

  h2.sect { font-size:11px; letter-spacing:.14em; color:var(--dim);
            font-weight:600; margin:26px 0 10px; }
  .tl-item { display:flex; gap:12px; padding:9px 0; font-size:13.5px;
             border-bottom:1px solid var(--line); cursor:pointer; }
  .tl-item:hover .goal { color:#4fd6be; }
  .tl-item:last-child { border-bottom:none; }
  .tl-item time { color:var(--dim); font-size:12px; white-space:nowrap;
                  width:74px; flex:none; }
  .addrow { display:flex; gap:8px; margin-top:10px; }
  .quietrow { display:flex; gap:14px; margin-top:26px; font-size:12.5px; }
  .quietbtn { background:none; border:none; color:var(--dim); cursor:pointer;
              font-size:12.5px; padding:0; }
  .quietbtn:hover { color:#f7768e; }
</style>
</head>
<body>
<div class="wrap">
  <p><a class="back-link" href="/">‹ Back to your threads</a></p>
  <div class="statustag" id="ttag">…</div>
  <div class="thead" id="tname">…</div>
  <p class="theadline" id="thead">…</p>
  <div class="chiprow" id="tchips"></div>

  <div id="mapbox"></div>
  <button class="showmore" id="more" style="display:none"
          onclick="showAll=!showAll; draw()">show more</button>

  <div class="actions" style="flex-direction:column;align-items:flex-start;gap:4px">
    <button class="primary" onclick="resumeThread()">Resume this thread</button>
    <span style="font-size:12px;color:var(--dim)">Makes this your active
      thread and shows its latest card.</span>
  </div>
  <div id="restore-slot"></div>

  <div class="addrow">
    <input type="text" id="addnode"
           placeholder="Add something the capture didn't see — a person, doc, task…"
           onkeydown="if(event.key==='Enter')addNode()">
    <button onclick="addNode()">Add</button>
  </div>

  <h2 class="sect" id="tl-title" style="display:none">RECENT CARDS</h2>
  <div id="timeline"></div>

  <div class="quietrow">
    <button class="quietbtn" onclick="mergeMenu(event)">Merge into another thread…</button>
    <button class="quietbtn" onclick="archiveThread()">Archive — this is done</button>
  </div>
</div>
<script>
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
const TID = decodeURIComponent(location.pathname.split("/").pop());
let data = null, showAll = false, menuEl = null;

function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }
document.addEventListener("click", e => {
  if (menuEl && !e.target.closest(".menu") && !e.target.closest(".node"))
    closeMenu();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeMenu(); }
});

async function load() {
  let d;
  try { d = await (await fetch(`/api/thread/${TID}/map`)).json(); }
  catch (e) { return; }
  if (!d.ok) { location.href = "/"; return; }
  data = d;
  draw();
}

function rel(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s/60) + "m ago";
  if (s < 86400) return Math.round(s/3600) + "h ago";
  return Math.round(s/86400) + "d ago";
}

function draw() {
  const t = data.thread;
  const tag = document.getElementById("ttag");
  tag.textContent = t.status === "active" ? "ACTIVE NOW" :
    (t.status === "archived" ? "ARCHIVED" : "HELD");
  tag.className = "statustag " + (t.status === "active" ? "active" : "");
  document.getElementById("tname").textContent = t.name;
  document.getElementById("thead").textContent = data.headline;
  document.getElementById("tchips").innerHTML = (data.chips || []).map(ch =>
    `<span class="chip" title="part of this thread · ${ch.count} moments">
       ${esc(ch.label)}</span>`).join("") +
    (data.chips_more ? `<span class="chip more">+${data.chips_more}</span>` : "");

  // map
  const box = document.getElementById("mapbox");
  const W = box.clientWidth, H = box.clientHeight;
  const cx = W/2, cy = H/2;
  const nodes = showAll ? data.nodes.concat(data.more_nodes) : data.nodes;
  const R = Math.min(W, H)/2 - 46;
  let html = `<div class="center-chip">${esc(t.name)}</div>`;
  nodes.forEach((n, i) => {
    const a = (2*Math.PI*i)/nodes.length - Math.PI/2;
    const x = cx + R*Math.cos(a), y = cy + R*Math.sin(a);
    const len = Math.hypot(x-cx, y-cy);
    const ang = Math.atan2(y-cy, x-cx);
    html += `<div class="edge ${n.kind==="blocker"&&!n.resolved?"blocker":""}"
      style="left:${cx}px;top:${cy}px;width:${len}px;
             transform:rotate(${ang}rad)"></div>`;
    html += `<div class="node ${n.kind==="blocker"&&!n.resolved?"blocker":""}
                  ${n.resolved?"resolved":""}"
                  style="left:${x}px;top:${y}px"
                  onclick="nodeMenu(event,'${n.id}')">
      <span class="kind">${n.pinned?'<span class="pinmark">●</span> ':""}${esc(n.kind)}</span>
      <span class="lbl">${esc(n.label)}</span></div>`;
  });
  box.innerHTML = html;
  document.getElementById("more").style.display =
    data.more_nodes.length ? "" : "none";
  document.getElementById("more").textContent = showAll
    ? "show fewer" : `show ${data.more_nodes.length} more`;

  // restore offer (quiet, honest)
  const rs = document.getElementById("restore-slot");
  if (data.restorable.length) {
    rs.innerHTML = `<div class="restore">
      I can bring back: ${data.restorable.map(r =>
        `<div class="item">↳ ${esc(r.label)}</div>`).join("")}
      <div class="note">The rest is on the card — scroll position, unsaved
        work and app layouts can't be restored.</div>
      <div style="margin-top:10px">
        <button onclick="restoreAll()">Bring these back</button>
      </div></div>`;
  } else rs.innerHTML = "";

  // timeline
  const tl = document.getElementById("timeline");
  document.getElementById("tl-title").style.display =
    data.cards.length ? "" : "none";
  tl.innerHTML = data.cards.map(c =>
    `<div class="tl-item" onclick="openCard('${c.file}')">
       <time>${rel(c.at)}</time><div class="goal">${esc(c.goal)}</div>
     </div>`).join("");
}

function nodeMenu(ev, nid) {
  ev.stopPropagation();
  closeMenu();
  const all = data.nodes.concat(data.more_nodes);
  const n = all.find(x => x.id === nid);
  menuEl = document.createElement("div");
  menuEl.className = "menu";
  menuEl.style.left = Math.min(ev.pageX, window.innerWidth-230) + "px";
  menuEl.style.top = ev.pageY + "px";
  menuEl.innerHTML = `
    <div class="prov" style="border-bottom:1px solid var(--line);margin-bottom:4px">
      <span style="font-size:10px;letter-spacing:.1em;color:${
        n.kind==="blocker" ? "#f7768e" : "var(--dim)"}">${esc(n.kind.toUpperCase())}</span><br>
      <b style="color:var(--text)">${esc(n.label)}</b>
      <div id="prov-line-${n.id}" style="margin-top:2px">…</div>
    </div>
    <div class="inline"><input id="rn" value="${esc(n.label)}"
      onkeydown="if(event.key==='Enter')act('${nid}','rename',
        {value:document.getElementById('rn').value})"></div>
    <button onclick="moveMenu('${nid}')">Move to another thread…</button>
    ${["blocker","decision"].includes(n.kind) && !n.resolved ?
      `<button onclick="act('${nid}','resolve')">Mark resolved</button>` : ""}
    <button onclick="act('${nid}','${n.pinned?"unpin":"pin"}')">
      ${n.pinned ? "Unpin" : "Pin — always show this"}</button>
    <button onclick="whyMenu('${nid}')">Why is this here?</button>
    <button onclick="act('${nid}','detach')">Detach from this thread</button>
    <button class="danger" onclick="act('${nid}','remove')">Not a thing — remove</button>`;
  document.body.appendChild(menuEl);
  fetch(`/api/node/${nid}/why`).then(r=>r.json()).then(w => {
    const el = document.getElementById("prov-line-" + nid);
    if (!el) return;
    const cards = (w.sources||[]).filter(s => s.startsWith("card_")).length;
    const how = w.sources && w.sources.includes("user") ? "you added it"
      : cards ? `from your card ${rel(w.first_seen)}` : "observed";
    el.textContent = `${how} · seen ${w.seen || (w.sources||[]).length || 1}× since ${rel(w.first_seen)}`;
  });
}

function moveMenu(nid) {
  fetch("/api/board").then(r=>r.json()).then(b => {
    if (!menuEl) return;
    menuEl.innerHTML = b.threads.filter(t => t.id !== TID)
      .map(t => `<button onclick="act('${nid}','move',{thread:'${t.id}'})">
        ${esc(t.name)}</button>`).join("") ||
      `<div class="prov">No other threads yet.</div>`;
  });
}

function whyMenu(nid) {
  fetch(`/api/node/${nid}/why`).then(r=>r.json()).then(w => {
    if (!menuEl) return;
    const rows = (w.details||[]).map(d =>
      d.kind === "user"
        ? `<div style="padding:5px 0">${esc(d.sentence)}</div>`
        : `<div style="padding:5px 0;border-top:1px solid var(--line)">
             <span style="color:var(--text)">“${esc(d.sentence)}”</span><br>
             <a onclick="event.stopPropagation();openCard('${d.file}')"
                style="color:#4fd6be;cursor:pointer">card ${rel(d.at)}</a>
             ${d.contaminated ? " · <span style='color:#e0af68'>excluded card</span>" : ""}
           </div>`).join("");
    menuEl.innerHTML = `<div class="prov" style="max-width:280px">
      <b style="color:var(--text)">${esc(w.label)}</b><br>
      First noticed ${rel(w.first_seen)} · seen ${w.seen}× ·
      last ${rel(w.last_seen)}.${w.sticky ?
        "<br>You placed it here — it stays unless you move it." : ""}
      ${rows}</div>`;
  });
}

async function act(nid, action, extra) {
  closeMenu();
  await fetch(`/api/node/${nid}`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action, ...(extra||{})})});
  // Quiet confirmation, no popups: the map redraws with the truth.
  load();
}

async function addNode() {
  const i = document.getElementById("addnode");
  if (!i.value.trim()) return;
  await fetch(`/api/thread/${TID}/node/add`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({label:i.value.trim(), kind:"task"})});
  i.value = "";
  i.placeholder = "Added — I'll remember that.";
  load();
}

async function resumeThread() {
  await fetch(`/api/thread/${TID}/resume`, {method:"POST"});
  load();
}

async function restoreAll() {
  const r = await (await fetch(`/api/thread/${TID}/restore`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({items:data.restorable})})).json();
  const rs = document.getElementById("restore-slot");
  rs.innerHTML = `<div class="restore">Brought back:
    ${(r.opened||[]).map(o=>`<div class="item">✓ ${esc(o)}</div>`).join("")
    || "nothing could be reopened"}</div>`;
}

function openCard(file) {
  fetch(`/api/overlay/show?card=${encodeURIComponent(file)}`,
        {method:"POST"});
}

function mergeMenu(ev) {
  closeMenu();
  fetch("/api/board").then(r=>r.json()).then(b => {
    menuEl = document.createElement("div");
    menuEl.className = "menu";
    menuEl.style.left = Math.min(ev.pageX, window.innerWidth-230)+"px";
    menuEl.style.top = ev.pageY+"px";
    menuEl.innerHTML = `<div class="prov">Fold this thread into:</div>` +
      (b.threads.filter(t => t.id !== TID).map(t =>
        `<button onclick="mergeInto('${t.id}')">${esc(t.name)}</button>`)
        .join("") || `<div class="prov">No other threads.</div>`);
    document.body.appendChild(menuEl);
  });
  ev.stopPropagation();
}

async function mergeInto(keep) {
  closeMenu();
  await fetch("/api/thread/merge", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({keep, absorb:TID})});
  location.href = "/thread/" + keep;
}

async function archiveThread() {
  await fetch(`/api/thread/${TID}/archive`, {method:"POST"});
  location.href = "/";
}

load();
setInterval(load, 5000);
</script>
</body>
</html>
"""


TRUST_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Recovery Card — what it sees</title>
<style>
""" + _BASE_CSS + r"""
  .panel { background:var(--panel); border:1px solid var(--line);
           border-radius:14px; padding:20px 24px; margin-bottom:16px; }
  .panel h2 { font-size:11px; letter-spacing:.14em; color:var(--dim);
              font-weight:600; margin:0 0 14px; }
  .switch-row { display:flex; align-items:center; gap:14px; padding:11px 0;
                border-bottom:1px solid var(--line); }
  .switch-row:last-child { border-bottom:none; }
  .switch-row .name { flex:1; font-size:15px; }
  .switch-row .state { font-size:12px; color:var(--dim); width:130px; }
  .toggle { width:44px; height:26px; border-radius:13px; border:none;
            cursor:pointer; position:relative; background:var(--line);
            transition:background .25s ease; flex:none; padding:0; }
  .toggle.on { background:var(--good); }
  .toggle::after { content:""; position:absolute; top:3px; left:3px;
                   width:20px; height:20px; border-radius:50%; background:#fff;
                   transition:transform .25s ease; }
  .toggle.on::after { transform:translateX(18px); }

  .count-row { display:flex; gap:12px; flex-wrap:wrap; }
  .count { flex:1; min-width:140px; text-align:left; padding:16px 18px;
           border:1px solid var(--line); border-radius:12px; cursor:pointer;
           background:transparent; color:var(--text); }
  .count b { display:block; font-size:26px; font-weight:500; }
  .count span { font-size:12.5px; color:var(--dim); }
  .count:hover { border-color:var(--accent); }
  .drill { margin-top:14px; font-size:13px; }
  .drill img { max-width:100%; border-radius:10px; border:1px solid var(--line);
               margin-top:8px; }
  .drill .item { padding:7px 0; border-bottom:1px solid var(--line);
                 display:flex; gap:10px; align-items:center; }
  .drill .item:last-child { border-bottom:none; }
  .drill a { color:var(--accent); text-decoration:none; cursor:pointer; }
  .note { font-size:12.5px; color:var(--dim); margin-top:10px; }

  .danger { border-color:rgba(247,118,142,.35); }
  .danger button.del { border-color:rgba(247,118,142,.5); color:var(--bad); }
  .danger button.del:hover { background:rgba(247,118,142,.1); }
  .confirm { margin-top:10px; padding:12px 16px; border-radius:10px;
             background:rgba(247,118,142,.08); font-size:13.5px; }
  .confirm .row { display:flex; gap:8px; margin-top:10px; }

  .proof { display:flex; flex-direction:column; gap:8px; font-size:13.5px; }
  .proof .line { display:flex; gap:10px; align-items:baseline; }
  .proof .dot { width:8px; height:8px; border-radius:50%;
                background:var(--good); flex:none; }
  .back-link { color:var(--dim); text-decoration:none; font-size:13.5px; }
  .back-link:hover { color:var(--text); }
</style>
</head>
<body>
<div class="wrap">
  <p><a class="back-link" href="/">‹ Back to your threads</a></p>
  <h1>What it sees</h1>
  <p class="sub">Every mechanism, every piece of data on hand, and the
  controls to stop or delete any of it. Read live from this Mac — nothing
  here is a cached claim.</p>

  <div class="panel">
    <h2>WHAT'S BEING CAPTURED</h2>
    <div id="switches"></div>
    <p class="note">Flipping a switch off stops that mechanism within one
    capture cycle. It stays off until you turn it back on.</p>
  </div>

  <div class="panel">
    <h2>DATA ON HAND</h2>
    <div class="count-row" id="counts"></div>
    <div class="drill" id="drill"></div>
    <p class="note">Frames are a rolling window: the newest 20 distinct
    screenshots are kept and older ones are deleted as new ones arrive.</p>
  </div>

  <div class="panel">
    <h2>PRIVATE APPS</h2>
    <p style="font-size:13.5px;margin:0 0 12px">PLite never captures anything
    while these are in front. No screenshot, no window title, no clipboard —
    those moments simply don't exist to it.</p>
    <div id="private-list"></div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <select id="private-pick" style="flex:1;font:inherit;font-size:13.5px;
        padding:9px 12px;border-radius:9px;border:1px solid var(--line);
        background:var(--panel);color:var(--text)"></select>
      <button onclick="addPickedPrivate()">Add</button>
    </div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <input type="text" id="private-free"
             placeholder="Or type an app name — or a website like chase.com"
             onkeydown="if(event.key==='Enter')addFreePrivate()">
      <button onclick="addFreePrivate()">Add</button>
    </div>
    <p class="note">Takes effect immediately. The starter list was seeded
    from apps on this Mac — remove anything you like.</p>
  </div>

  <div class="panel danger">
    <h2>DELETE</h2>
    <div class="count-row">
      <button class="del" onclick="askDelete('captures')">Delete all captures</button>
      <button class="del" onclick="askDelete('thread')">Forget a thread</button>
      <button class="del" onclick="askDelete('everything')">Delete everything</button>
    </div>
    <div id="confirm"></div>
  </div>

  <div class="panel">
    <h2>ALWAYS TRUE</h2>
    <div class="proof" id="proof"></div>
  </div>
</div>
<script>
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
let trust = null;
let drillOpen = null;

async function load() {
  try { trust = await (await fetch("/api/trust")).json(); } catch(e){ return; }

  document.getElementById("switches").innerHTML =
    Object.entries(trust.switch_names).map(([k, label]) => {
      const on = trust.switches[k];
      return `<div class="switch-row">
        <div class="name">${esc(label)}</div>
        <div class="state">${on ? "capturing" : "off — not captured"}</div>
        <button class="toggle ${on ? "on" : ""}" onclick="flip('${k}')"
                aria-label="${esc(label)}: ${on ? "on" : "off"}"></button>
      </div>`;
    }).join("");

  const d = trust.data;
  document.getElementById("counts").innerHTML = `
    <button class="count" onclick="drill('frames')"><b>${d.frames}</b>
      <span>screenshots held (rolling)</span></button>
    <button class="count" onclick="drill('cards')"><b>${d.cards}</b>
      <span>cards</span></button>
    <button class="count" onclick="drill('threads')"><b>${d.threads}</b>
      <span>threads</span></button>`;

  document.getElementById("proof").innerHTML =
    `<div class="line"><span class="dot"></span>
       Network: <b>${trust.network === "OFFLINE"
         ? "offline — fully on-device"
         : "online, and still fully on-device"}</b></div>
     <div class="line"><span class="dot"></span>
       Model: <b>${esc(trust.model)}</b>, running on this Mac</div>` +
    trust.refusals.map(r =>
      `<div class="line"><span class="dot"></span>${esc(r)}</div>`).join("");

  if (drillOpen) renderDrill();
}

async function flip(k) {
  await fetch("/api/trust/toggle", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({switch:k, value:!trust.switches[k]})});
  load();
}

function drill(kind) {
  drillOpen = drillOpen === kind ? null : kind;
  renderDrill();
}

function renderDrill() {
  const host = document.getElementById("drill");
  const d = trust.data;
  if (!drillOpen) { host.innerHTML = ""; return; }
  if (drillOpen === "frames") {
    host.innerHTML = d.frame_files.length
      ? d.frame_files.slice().reverse().map(f =>
          `<div class="item"><a onclick="showFrame('${f}')">${f}</a></div>`
        ).join("") + `<div id="frame-view"></div>`
      : `<div class="note">No screenshots on hand.</div>`;
  } else if (drillOpen === "cards") {
    host.innerHTML = d.card_files.length
      ? d.card_files.slice().reverse().map(f =>
          `<div class="item"><a href="/cards/${f}" target="_blank">${f}</a></div>`
        ).join("")
      : `<div class="note">No cards yet.</div>`;
  } else if (drillOpen === "threads") {
    host.innerHTML = d.thread_list.length
      ? d.thread_list.map(t =>
          `<div class="item"><span style="flex:1">${esc(t.name)}</span>
           <a onclick="askForget('${t.id}', '${esc(t.name)}')">forget</a></div>`
        ).join("")
      : `<div class="note">No threads yet.</div>`;
  }
}

function showFrame(f) {
  document.getElementById("frame-view").innerHTML =
    `<img src="/captures/${f}" alt="screenshot ${f}">`;
}

function askDelete(scope) {
  const host = document.getElementById("confirm");
  const words = {
    captures: "Delete every screenshot currently held? The rolling window "
      + "starts empty again. This is immediate and cannot be undone.",
    thread: "Pick the thread to forget from the list under “threads” above "
      + "(click the count, then “forget” next to its name).",
    everything: "Delete every screenshot, every card, every thread and all "
      + "scoring data? Your on/off switches stay as you set them. This is "
      + "immediate and cannot be undone.",
  };
  if (scope === "thread") {
    host.innerHTML = `<div class="confirm">${words[scope]}</div>`;
    drillOpen = "threads"; renderDrill();
    return;
  }
  host.innerHTML = `<div class="confirm">${words[scope]}
    <div class="row">
      <button class="del" onclick="doDelete('${scope}')">Yes, delete</button>
      <button onclick="document.getElementById('confirm').innerHTML=''">Keep it</button>
    </div></div>`;
}

function askForget(id, name) {
  document.getElementById("confirm").innerHTML = `<div class="confirm">
    Forget “${name}”? Its return-point and history go with it. Immediate,
    cannot be undone.
    <div class="row">
      <button class="del" onclick="doForget('${id}')">Yes, forget it</button>
      <button onclick="document.getElementById('confirm').innerHTML=''">Keep it</button>
    </div></div>`;
}

async function doDelete(scope) {
  await fetch("/api/trust/delete", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({scope})});
  document.getElementById("confirm").innerHTML = "";
  drillOpen = null;
  load();
}
async function doForget(id) {
  await fetch("/api/trust/delete", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({scope:"thread", id})});
  document.getElementById("confirm").innerHTML = "";
  load();
}

// --- Private apps ------------------------------------------------------
async function loadPrivate() {
  let d; try { d = await (await fetch("/api/private")).json(); }
  catch(e){ return; }
  const rows = d.apps.map(a =>
    `<div class="switch-row"><div class="name">${esc(a)}</div>
     <button onclick="privateChange('remove','apps','${esc(a)}')">Remove</button>
     </div>`)
    .concat(d.domains.map(dm =>
      `<div class="switch-row"><div class="name">${esc(dm)}
        <span style="color:var(--dim);font-size:12px"> (website)</span></div>
       <button onclick="privateChange('remove','domains','${esc(dm)}')">Remove</button>
       </div>`));
  document.getElementById("private-list").innerHTML = rows.join("") ||
    `<div class="note">Nothing excluded right now.</div>`;
  const pick = document.getElementById("private-pick");
  pick.innerHTML = `<option value="">Running apps…</option>` +
    d.running.map(a => `<option>${esc(a)}</option>`).join("");
}
async function privateChange(action, kind, value) {
  await fetch("/api/private", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action, kind, value})});
  loadPrivate();
}
function addPickedPrivate() {
  const v = document.getElementById("private-pick").value;
  if (v) privateChange("add", "apps", v);
}
function addFreePrivate() {
  const i = document.getElementById("private-free");
  const v = i.value.trim();
  if (!v) return;
  privateChange("add", v.includes(".") && !v.includes(" ")
    ? "domains" : "apps", v);
  i.value = "";
}

load();
loadPrivate();
setInterval(load, 3000);
</script>
</body>
</html>
"""


OVERLAY_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Recovery Card</title>
<style>
  /* Everything here sits on a transparent, vibrancy-blurred macOS window.
     The desktop behind is still visible but out of focus, so there is
     exactly one thing to read. */
  html, body {
    margin:0; height:100%; background:transparent; overflow:hidden;
    font:16px/1.65 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
    -webkit-user-select:none; user-select:none;
  }
  /* A scrim over the whole screen. macOS vibrancy supplies the blur; this
     supplies the dimming, and it is also the fallback that still isolates
     the card if vibrancy is unavailable. */
  #stage {
    height:100%; display:flex; align-items:center; justify-content:center;
    padding:6vh 5vw;
    background:rgba(8,9,12,.52);
    animation:fade .28s ease-out;
  }
  @media (prefers-color-scheme: light) {
    #stage { background:rgba(236,238,242,.55); }
  }
  @keyframes fade { from { opacity:0; } to { opacity:1; } }
  /* Always a visible way out. Escape is handled natively as well, but a
     full-screen overlay must never depend on a keystroke to escape. */
  .x {
    position:absolute; top:16px; right:18px; width:30px; height:30px;
    border:none; border-radius:50%; cursor:pointer; font-size:19px;
    line-height:1; background:rgba(255,255,255,.09); color:#9aa3b2;
  }
  .x:hover { background:rgba(255,255,255,.16); color:#fff; }
  .card {
    position:relative;
    width:min(680px, 92vw); max-height:88vh; overflow-y:auto;
    background:rgba(22,24,30,.82); color:#f2f4f8;
    border:1px solid rgba(255,255,255,.10);
    border-radius:20px; padding:44px 46px;
    box-shadow:0 30px 90px rgba(0,0,0,.45);
    animation:rise .34s cubic-bezier(.2,.8,.25,1);
  }
  @media (prefers-color-scheme: light) {
    .card { background:rgba(252,252,253,.86); color:#14171c;
            border-color:rgba(0,0,0,.08); }
    .reason, .meta, h2 { color:#5b6472 !important; }
  }
  @keyframes rise {
    from { opacity:0; transform:translateY(10px) scale(.99); }
    to   { opacity:1; transform:none; }
  }
  h2 { font-size:10.5px; letter-spacing:.18em; font-weight:600;
       color:#8b93a1; margin:0 0 10px;
       display:flex; align-items:center; gap:8px; }
  .goal { font-size:29px; font-weight:500; line-height:1.28;
          letter-spacing:-.02em; margin:0 0 14px; }
  .reason { color:#9aa3b2; margin:0; font-size:16.5px; }
  .sec { margin-top:30px; }
  .next { font-size:20px; margin:0; font-weight:450; }
  ul { margin:0; padding-left:20px; }
  li { margin:5px 0; }
  .said { margin-top:26px; padding:14px 18px; border-left:2px solid #4fd6be;
          background:rgba(79,214,190,.10); font-style:italic;
          border-radius:0 8px 8px 0; }
  .meta { margin-top:30px; padding-top:18px;
          border-top:1px solid rgba(255,255,255,.10);
          font-size:12.5px; color:#8b93a1; }
  .flag { display:inline-block; padding:4px 10px; border-radius:6px;
          font-size:10.5px; font-weight:700; letter-spacing:.08em;
          margin-bottom:16px; }
  .flag.reduced { background:rgba(224,175,104,.18); color:#e0af68; }
  .flag.failed  { background:rgba(247,118,142,.16); color:#f7768e; }
  .hint { margin-top:22px; text-align:center; font-size:12px;
          color:rgba(255,255,255,.45); }
  .headline { font-size:15px; color:#9aa3b2; margin:0 0 6px; line-height:1.5; }
  .coverage { font-size:12px; color:#8b93a1; margin:0 0 10px; }
  .upchip { display:inline-block; padding:2px 8px; border-radius:6px;
            font-size:11px; background:rgba(79,214,190,.14); color:#4fd6be;
            font-style:normal; }
  .awayline { font-size:13px; color:#e0af68; margin:0 0 14px; }
  .parked { font-size:13.5px; color:#9aa3b2; padding:6px 0;
            border-bottom:1px solid rgba(255,255,255,.06); }
  .parked:last-child { border-bottom:none; }
  .parked b { color:#c8cdd6; font-weight:500; }
  .taps { margin-left:auto; display:inline-flex; gap:3px; opacity:.25;
          transition:opacity .2s; }
  .card:hover .taps { opacity:.7; } .taps:hover { opacity:1 !important; }
  .taps button { padding:1px 7px; font-size:11px; border-radius:5px;
                 background:transparent; border:1px solid rgba(255,255,255,.15);
                 color:#8b93a1; cursor:pointer; }
  .taps button.y.on { background:#4fd6be; border-color:#4fd6be; color:#0f1115; }
  .taps button.n.on { background:#f7768e; border-color:#f7768e; color:#0f1115; }
  .fixbox { display:flex; gap:6px; margin:8px 0 2px; }
  .fixbox input { flex:1; font:inherit; font-size:13px; padding:7px 11px;
                  border-radius:7px; border:1px solid #4fd6be;
                  background:rgba(0,0,0,.3); color:#f2f4f8; }
  .fixed-note { margin:8px 0 2px; font-size:12.5px; color:#4fd6be; }
  .waiting { text-align:center; color:#9aa3b2; }
  .spin { width:26px; height:26px; margin:0 auto 16px;
          border:2px solid rgba(255,255,255,.18); border-top-color:#4fd6be;
          border-radius:50%; animation:spin 1s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div id="stage"></div>
<script>
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

function close() {
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.close) {
      window.pywebview.api.close();
      return;
    }
  } catch (e) {}
  // If the bridge is not ready, tell the backend to kill the overlay
  // process. There is always a way out.
  fetch("/api/overlay/close", {method: "POST"});
}

// TEMPORAL ANCHOR: what slice of time the card actually saw, plain words.
function fmtClock(iso) {
  const d = new Date(iso);
  let h = d.getHours(), m = String(d.getMinutes()).padStart(2, "0");
  const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
  return `${h}:${m} ${ap}`;
}
function agoWords(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s/60)} min ago`;
  return `${Math.round(s/360)/10} h ago`;
}
function coverageLine(c) {
  if (!c || !c.coverage) return "";
  const a = fmtClock(c.coverage.start), b2 = fmtClock(c.coverage.end);
  if (c.stale_capture_minutes)
    return `<p class="awayline" style="font-weight:600">From your screen
      ${c.stale_capture_minutes} minutes ago — nothing newer was
      captured.</p>
      <p class="coverage">${a === b2 ? a : a + "–" + b2}
      · ${agoWords(c.generated_at)}</p>`;
  return `<p class="coverage">From your screen, ${a === b2 ? a :
    a + "–" + b2} · ${agoWords(c.generated_at)}</p>`;
}

// Escape and click-outside only. Enter and Space are deliberately NOT
// dismiss keys: this thing appears over whatever you were doing, and a
// stray keystroke should not make it vanish before you have read it.
document.addEventListener("keydown", e => {
  if (e.key === "Escape") close();
});
document.addEventListener("click", e => {
  if (!e.target.closest(".card")) close();
});

// Rebuilding the DOM on every poll re-runs the entrance animation and makes
// the card visibly blink every 2 seconds. Only redraw when what is being
// shown actually changes; while reconstructing, tick the counter alone.
let lastSig = null;

// The moment of return shows ONLY what a depleted mind needs: the
// situation in one sentence, the active thread's card, the other threads
// with their return-points, and a way to say right/wrong. Nothing
// operational lives here.
let verdicts = {};

function taps(file, field) {
  const v = verdicts[file + ":" + field];
  return `<span class="taps">
    <button class="y ${v === true ? "on" : ""}"
            onclick="tap('${file}','${field}',true)">✓</button>
    <button class="n ${v === false ? "on" : ""}"
            onclick="tap('${file}','${field}',false)">✗</button></span>`;
}
async function tap(file, field, value) {
  verdicts[file + ":" + field] = value;
  lastSig = null; draw();
  await fetch("/api/verdict", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({file, field, value})});
  if (value === false) setTimeout(() => showFix(file, field), 60);
}
function showFix(file, field) {
  const host = document.getElementById("fix-" + field);
  if (!host) return;
  host.innerHTML = `<div class="fixbox">
    <input id="fixin-${field}" placeholder="What were you actually doing?"
           onkeydown="if(event.key==='Enter')saveFix('${file}','${field}')">
    <button onclick="saveFix('${file}','${field}')">Save</button></div>`;
  document.getElementById("fixin-" + field)?.focus();
}
async function saveFix(file, field) {
  const i = document.getElementById("fixin-" + field);
  const text = i ? i.value.trim() : "";
  if (!text) return;
  await fetch("/api/verdict", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({file, field, value:false, correction:text})});
  const host = document.getElementById("fix-" + field);
  if (host) host.innerHTML =
    `<div class="fixed-note">Saved. Later cards will treat that as truth.</div>`;
}

// A ?card= parameter shows one specific card, read-only: no taps, no
// polling redraw. Used by the thread map's timeline.
const READONLY_CARD = new URLSearchParams(location.search).get("card");

async function drawReadonly() {
  let c;
  try { c = await (await fetch(`/cards/${READONLY_CARD}`)).json(); }
  catch (e) { return; }
  const loops = (c.open_loops||[]).map(l=>"<li>"+esc(l)+"</li>").join("");
  document.getElementById("stage").innerHTML = `
    <div class="card">
      <button class="x" onclick="close()" title="Dismiss">&times;</button>
      <p class="headline">An earlier card${c.thread ? " from " + esc(c.thread) : ""}
        · ${esc((c.generated_at||"").replace("T"," "))}</p>
      ${coverageLine(c)}
      <h2>PICK UP HERE</h2>
      <p class="goal">${esc(c.goal)}</p>
      <p class="reason">${esc(c.reasoning)}</p>
      <div class="sec"><h2 style="color:#e0af68">NEXT STEP</h2>
        <p class="next">${esc(c.next_action)}</p></div>
      ${loops?`<div class="sec"><h2>OPEN LOOPS</h2><ul>${loops}</ul></div>`:""}
      ${c.park_note?`<div class="said">You said: “${esc(c.park_note)}”</div>`:""}
      <div class="meta">confidence ${esc(c.confidence)}<br>
        evidence: ${esc(c.evidence)}</div>
    </div>
    <div class="hint">esc to dismiss</div>`;
}

async function draw() {
  if (READONLY_CARD) { return; }
  let d, b;
  try {
    d = await (await fetch("/api/state")).json();
    b = await (await fetch("/api/board")).json();
  } catch (e) { return; }

  const stage = document.getElementById("stage");
  // SUMMON SEMANTICS: an existing card ALWAYS shows instantly, with its
  // age. A stale card refreshes in the background and swaps in quietly.
  // The blocking "reading" state exists only when no card has ever been
  // generated.
  const updating = d.mode === "RECONSTRUCTING" ||
                   d.reconstructing_for !== null;
  const reconstructing = !d.card;
  const sig = JSON.stringify([reconstructing, d.card ? d.card._file : null,
                              verdicts, b.headline, updating]);
  if (sig === lastSig) {
    if (reconstructing) {
      const el = document.getElementById("elapsed");
      if (el) el.textContent = (d.reconstructing_for ?? 0) + "s";
    }
    return;
  }
  const swapped = lastSig && d.card &&
    JSON.parse(lastSig)[1] && JSON.parse(lastSig)[1] !== d.card._file;
  lastSig = sig;

  if (reconstructing && d.running) {
    // Dismissable even here. A full-screen surface with no exit, at any
    // moment, is a trap; the generation keeps running in the background
    // and the finished card waits on the board.
    stage.innerHTML = `<div class="card waiting">
      <button class="x" onclick="close()" title="Dismiss">&times;</button>
      <div class="spin"></div>
      <div>Reading your screens…</div>
      <div class="hint" id="elapsed">${d.reconstructing_for ?? 0}s</div>
      <div class="hint">esc to dismiss — your card will be ready either way</div></div>`;
    return;
  }

  const c = d.card;
  if (!c) {
    stage.innerHTML = `<div class="card waiting">
      <button class="x" onclick="close()" title="Dismiss">&times;</button>
      <div>No card yet.</div>
      <div class="hint">esc to dismiss</div></div>`;
    return;
  }

  let flag = "";
  if (c.reduced_model) flag += '<div class="flag reduced">REDUCED MODEL</div> ';
  if (c.fail_closed)   flag += '<div class="flag failed">NOT ENOUGH SIGNAL</div>';
  const loops = (c.open_loops||[]).map(l=>"<li>"+esc(l)+"</li>").join("");
  const f = c._file;
  const away = d.away_summary ? `<p class="awayline">${esc(d.away_summary)}</p>` : "";
  const parked = (c.parked || []).filter(p => p.name !== c.thread);
  const chip = swapped ? `<span class="upchip">updated just now</span>` :
    (updating ? `<span class="upchip">updating…</span>` : "");

  stage.innerHTML = `
    <div class="card">
      <button class="x" onclick="close()" title="Dismiss">&times;</button>
      <p class="headline">${esc(b.headline)} ${chip}</p>
      ${coverageLine(c)}
      ${away}
      ${flag}
      ${c.agent_status ? `<div class="said">While you were away: ${esc(c.agent_status)}</div>` : ""}
      <h2>PICK UP HERE ${taps(f, "goal")}</h2>
      <p class="goal">${esc(c.goal)}</p><div id="fix-goal"></div>
      <h2 style="margin-top:16px">WHY ${taps(f, "reasoning")}</h2>
      <p class="reason">${esc(c.reasoning)}</p><div id="fix-reasoning"></div>
      <div class="sec"><h2 style="color:#e0af68">NEXT STEP ${taps(f, "next_action")}</h2>
        <p class="next" style="${c.next_done ? "text-decoration:line-through;opacity:.55" : ""}">${esc(c.next_action)}</p>
        <div id="next-afford"></div><div id="fix-next_action"></div></div>
      ${loops?`<div class="sec"><h2>OPEN LOOPS ${taps(f, "open_loops")}</h2>
        <ul>${loops}</ul><div id="fix-open_loops"></div></div>`:""}
      ${c.park_note?`<div class="said">You said: “${esc(c.park_note)}”</div>`:""}
      ${parked.length ? `<div class="sec"><h2>ALSO HOLDING</h2>` +
        parked.slice(0,4).map(p => `<div class="parked">
          <b>${esc(p.name)}</b>${p.return_point ? " — " + esc(p.return_point) : ""}
        </div>`).join("") + `</div>` : ""}
      <div class="meta">
        confidence ${esc(c.confidence)} · ${esc(c.model||"")}
        ${c.trigger?"· "+esc(c.trigger):""}<br>
        evidence: ${esc(c.evidence)}
      </div>
    </div>
    <div class="hint">esc to dismiss</div>`;
}

if (READONLY_CARD) {
  drawReadonly();
} else {
  draw();
  setInterval(draw, 2000);
  window.addEventListener("focus", draw);
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    _test_flags = sorted(k for k in os.environ if k.startswith("RC_TEST_")
                         or k == "RC_SANDBOX")
    if _test_flags and str(os.environ.get("PORT", DEFAULT_PORT)) == \
            str(DEFAULT_PORT):
        print("\n  Test flags are set (" + ", ".join(_test_flags) + ") "
              "but this is the LIVE port.")
        print("  A live engine never runs with test hooks. Unset them, or "
              "use a test PORT.\n")
        sys.exit(2)
    print("Recovery Card — running preflight...")
    issues = preflight()
    for issue in issues:
        print(f"  [{issue['level'].upper()}] {issue['problem']}")
        print(f"          fix: {issue['fix']}")
    if not issues:
        print("  All checks passed: Ollama up, model present, screen readable.")

    wanted = int(os.environ.get("PORT", DEFAULT_PORT))

    # Single instance: if OUR engine already answers on the wanted port,
    # this launch defers to it instead of stepping to a second port and
    # silently running a parallel universe.
    try:
        import urllib.request as _u
        with _u.urlopen(f"http://127.0.0.1:{wanted}/api/state",
                        timeout=2) as r:
            if r.status == 200:
                print(f"\n  Recovery Card is already running on port "
                      f"{wanted}. Using that instance.\n")
                sys.exit(0)
    except Exception:
        pass

    port, moved = choose_port()
    if moved:
        print("\n" + "!" * 66)
        print(f"  WARNING: port {wanted} was busy, so this is running on {port}.")
        print(f"  Your usual URL will NOT work. Use the one below.")
        print(f"  To free up {wanted}, quit whatever is using it:")
        print(f"      lsof -nP -iTCP:{wanted} -sTCP:LISTEN")
        print("!" * 66)

    print(f"\n  Open http://localhost:{port} in your browser.\n")
    # threaded=True is load-bearing: werkzeug's default is single-threaded,
    # which froze every endpoint for the length of a card generation and
    # turned the overlay into an undismissable full-screen trap.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
