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
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

import capture
import card as card_mod

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"
CARDS = ROOT / "cards"
STATUS = CAPTURES / "status.json"
PARK_NOTE = CARDS / "park_note.json"

app = Flask(__name__)

# The capture subprocess we launched, if any.
_capture_proc = None
# Set while an on-demand card generation is running.
_generating_since = None


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
    return s


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


def load_card(path):
    try:
        c = json.loads(path.read_text())
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
    return render_template_string(PAGE)


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
    """The 'I'm back' path when no card is waiting."""
    global _generating_since
    if _generating_since is not None:
        return jsonify({"ok": False, "note": "already generating"})
    _generating_since = time.time()
    os.environ["RECOVERY_TRIGGER"] = "im_back"  # provenance, see card.py
    try:
        card_mod.generate()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _generating_since = None
        os.environ.pop("RECOVERY_TRIGGER", None)


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

    _generating_since = time.time()
    os.environ["RECOVERY_TRIGGER"] = "hotkey"
    try:
        card_mod.generate()
        return jsonify({"ok": True, "generated": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _generating_since = None
        os.environ.pop("RECOVERY_TRIGGER", None)


_overlay_proc = None


def open_overlay():
    global _overlay_proc
    if _overlay_proc and _overlay_proc.poll() is None:
        return
    _overlay_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "overlay.py"),
         f"http://localhost:{request.host.split(':')[-1]}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.route("/api/overlay/close", methods=["POST"])
def api_overlay_close():
    """Last-resort dismissal. A full-screen overlay must never be a trap."""
    global _overlay_proc
    killed = False
    if _overlay_proc and _overlay_proc.poll() is None:
        _overlay_proc.terminate()
        killed = True
    else:
        subprocess.run(["pkill", "-f", "overlay.py"], capture_output=True)
        killed = True
    return jsonify({"ok": killed})


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
    --text:#e6e8ec; --dim:#8b93a1; --accent:#7aa2f7;
    --good:#7bcf9e; --warn:#e0af68; --bad:#f7768e;
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
  }
  .goal { font-size:23px; font-weight:500; line-height:1.35; margin:0 0 10px; letter-spacing:-.01em; }
  .reason { color:var(--dim); margin:0; }
  .sec { margin-top:24px; }
  .next { font-size:17px; margin:0; }
  ul { margin:0; padding-left:20px; }
  li { margin:3px 0; }
  .said {
    margin-top:22px; padding:12px 16px; border-left:2px solid var(--accent);
    background:rgba(122,162,247,.07); font-style:italic;
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

  <div class="hist" id="hist"></div>
</div>

<script>
let running = false;

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

function renderCard(c) {
  const slot = document.getElementById("card-slot");
  if (!c) { slot.innerHTML = ""; return; }

  let flag = "";
  if (c.reduced_model)
    flag += '<div class="flag reduced">REDUCED MODEL — low confidence</div> ';
  if (c.fail_closed)
    flag += '<div class="flag failed">NOT ENOUGH SIGNAL</div>';

  const loops = (c.open_loops || []).map(l => "<li>" + esc(l) + "</li>").join("");

  slot.innerHTML = `
    <div class="card">
      ${flag}
      <h2>PICK UP HERE</h2>
      <p class="goal">${esc(c.goal)}</p>
      <p class="reason">${esc(c.reasoning)}</p>
      <div class="sec">
        <h2>NEXT STEP</h2>
        <p class="next">${esc(c.next_action)}</p>
      </div>
      ${loops ? `<div class="sec"><h2>OPEN LOOPS</h2><ul>${loops}</ul></div>` : ""}
      ${c.park_note ? `<div class="said">You said: “${esc(c.park_note)}”</div>` : ""}
      <div class="meta">
        confidence ${esc(c.confidence)} · ${esc(c.model || "")}
        ${c.trigger ? "· triggered by " + esc(c.trigger) : ""}<br>
        evidence: ${esc(c.evidence)}
      </div>
    </div>`;
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
setInterval(tick, 2000);
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
       color:#8b93a1; margin:0 0 10px; }
  .goal { font-size:29px; font-weight:500; line-height:1.28;
          letter-spacing:-.02em; margin:0 0 14px; }
  .reason { color:#9aa3b2; margin:0; font-size:16.5px; }
  .sec { margin-top:30px; }
  .next { font-size:20px; margin:0; font-weight:450; }
  ul { margin:0; padding-left:20px; }
  li { margin:5px 0; }
  .said { margin-top:26px; padding:14px 18px; border-left:2px solid #7aa2f7;
          background:rgba(122,162,247,.10); font-style:italic;
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
  .waiting { text-align:center; color:#9aa3b2; }
  .spin { width:26px; height:26px; margin:0 auto 16px;
          border:2px solid rgba(255,255,255,.18); border-top-color:#7aa2f7;
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

async function draw() {
  let d;
  try { d = await (await fetch("/api/state")).json(); }
  catch (e) { return; }

  const stage = document.getElementById("stage");
  const reconstructing = d.mode === "RECONSTRUCTING" || (!d.card && d.running);
  const sig = JSON.stringify([reconstructing, d.card ? d.card._file : null]);

  if (sig === lastSig) {
    if (reconstructing) {
      const el = document.getElementById("elapsed");
      if (el) el.textContent = (d.reconstructing_for ?? 0) + "s";
    }
    return;
  }
  lastSig = sig;

  if (reconstructing) {
    stage.innerHTML = `<div class="card waiting"><div class="spin"></div>
      <div>Reading your screens…</div>
      <div class="hint" id="elapsed">${d.reconstructing_for ?? 0}s</div></div>`;
    return;
  }

  const c = d.card;
  if (!c) {
    stage.innerHTML = `<div class="card waiting">
      <div>No card yet.</div>
      <div class="hint">esc to dismiss</div></div>`;
    return;
  }

  let flag = "";
  if (c.reduced_model) flag += '<div class="flag reduced">REDUCED MODEL</div> ';
  if (c.fail_closed)   flag += '<div class="flag failed">NOT ENOUGH SIGNAL</div>';
  const loops = (c.open_loops||[]).map(l=>"<li>"+esc(l)+"</li>").join("");

  stage.innerHTML = `
    <div class="card">
      <button class="x" onclick="close()" title="Dismiss">&times;</button>
      ${flag}
      <h2>PICK UP HERE</h2>
      <p class="goal">${esc(c.goal)}</p>
      <p class="reason">${esc(c.reasoning)}</p>
      <div class="sec"><h2>NEXT STEP</h2><p class="next">${esc(c.next_action)}</p></div>
      ${loops?`<div class="sec"><h2>OPEN LOOPS</h2><ul>${loops}</ul></div>`:""}
      ${c.park_note?`<div class="said">You said: “${esc(c.park_note)}”</div>`:""}
      <div class="meta">
        confidence ${esc(c.confidence)} · ${esc(c.model||"")}
        ${c.trigger?"· triggered by "+esc(c.trigger):""}<br>
        evidence: ${esc(c.evidence)}
      </div>
    </div>
    <div class="hint">esc to dismiss</div>`;
}

draw();
setInterval(draw, 2000);
window.addEventListener("focus", draw);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("Recovery Card — running preflight...")
    issues = preflight()
    for issue in issues:
        print(f"  [{issue['level'].upper()}] {issue['problem']}")
        print(f"          fix: {issue['fix']}")
    if not issues:
        print("  All checks passed: Ollama up, model present, screen readable.")

    wanted = int(os.environ.get("PORT", DEFAULT_PORT))
    port, moved = choose_port()
    if moved:
        print("\n" + "!" * 66)
        print(f"  WARNING: port {wanted} was busy, so this is running on {port}.")
        print(f"  Your usual URL will NOT work. Use the one below.")
        print(f"  To free up {wanted}, quit whatever is using it:")
        print(f"      lsof -nP -iTCP:{wanted} -sTCP:LISTEN")
        print("!" * 66)

    print(f"\n  Open http://localhost:{port} in your browser.\n")
    app.run(host="127.0.0.1", port=port, debug=False)
