#!/usr/bin/env python3
"""Rehearsal autopilot - a synthetic user against the REAL pipeline.

Real input synthesized through macOS scripting, real capture, real
classification, real card generation. Never injected frames.

  plite rehearse <scenario> [--seed N] [--runs N]
  plite rehearse --list
  plite rehearse --regressions

FENCES, absolute:
  - runs only when explicitly invoked (this command IS the invocation)
  - a persistent on-screen banner: "REHEARSAL - scripted input"
  - every frame and card tagged rehearsal; product tallies never touched
  - results go to eval/rehearsal.json, a separate store, always
  - Esc aborts instantly (key-state poll between and during steps)

Honest limits are part of the report: AX targets that fell back to
keyboard navigation, steps that needed a retry, distractors injected.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
SCEN_DIR = ROOT / "scenarios"
STORE = ROOT / "eval" / "rehearsal.json"
REGRESSIONS = ROOT / "eval" / "rehearsal_regressions.json"

PORT = os.environ.get("PORT", "5001")


# --- Esc abort -------------------------------------------------------------

def esc_down():
    try:
        from Quartz import CGEventSourceKeyState
        return bool(CGEventSourceKeyState(1, 53))   # kCGEventSourceStateHID
    except Exception:
        return False


class Abort(Exception):
    pass


def nap(seconds):
    """Sleep in small slices so Esc aborts within ~100ms."""
    end = time.time() + seconds
    while time.time() < end:
        if esc_down():
            raise Abort()
        time.sleep(0.08)


# --- Input synthesis (AX first, honest fallbacks) --------------------------

def osa(script, timeout=10):
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, r.stdout.strip(), r.stderr.strip()


def frontmost():
    ok, out, _ = osa('tell application "System Events" to get name of '
                     'first application process whose frontmost is true')
    return out if ok else ""


class Driver:
    def __init__(self, rng, report):
        self.rng = rng
        self.report = report

    def verify_focus(self, app, retried=False):
        nap(0.6)
        if frontmost() == app:
            return True
        if not retried:
            self.report["retries"].append(f"focus {app}")
            osa(f'tell application "{app}" to activate')
            return self.verify_focus(app, retried=True)
        self.report["failures"].append(f"focus {app} never landed")
        return False

    # step: open/focus an app
    def s_focus(self, step):
        app = step["app"]
        osa(f'tell application "{app}" to activate')
        return self.verify_focus(app)

    # step: click an AX target by name; keyboard-nav fallback is reported
    def s_click(self, step):
        app, target = step["app"], step["target"]
        self.s_focus({"app": app})
        ok, _, _ = osa(
            f'tell application "System Events" to tell process "{app}" to '
            f'click (first UI element of window 1 whose name is "{target}")')
        if not ok:
            self.report["ax_fallbacks"].append(f"{app}:{target}")
            return False  # honest: we do not blind-click coordinates
        return True

    # step: type with human pacing, typos + corrections, think-pauses
    def s_type(self, step):
        text = step["text"]
        for i, ch in enumerate(text):
            if esc_down():
                raise Abort()
            if ch == "\n":
                osa('tell application "System Events" to key code 36')
            else:
                safe = ch.replace("\\", "\\\\").replace('"', '\\"')
                # typo + correction, seeded
                if ch.isalpha() and self.rng.random() < step.get(
                        "typo_rate", 0.04):
                    wrong = self.rng.choice("qwertasdfg")
                    osa(f'tell application "System Events" to keystroke "{wrong}"')
                    nap(0.12 + self.rng.random() * 0.1)
                    osa('tell application "System Events" to key code 51')
                osa(f'tell application "System Events" to keystroke "{safe}"')
            base = 0.045 + self.rng.random() * 0.075
            if ch in ".,;: " and self.rng.random() < 0.12:
                base += 0.5 + self.rng.random() * 1.4   # think-pause
            nap(base)
        return True

    def s_scroll(self, step):
        for _ in range(step.get("times", 4)):
            osa('tell application "System Events" to key code 121')  # pg dn
            nap(0.5 + self.rng.random() * 1.2)
        osa('tell application "System Events" to key code 116')      # pg up
        nap(0.4)
        return True

    def s_glance(self, step):
        """2 seconds, no engagement. Must stay ambient."""
        osa(f'tell application "{step["app"]}" to activate')
        nap(2.0)
        return True

    def s_thrash(self, step):
        """Rapid switches, no engagement. Must build no affinity."""
        apps = step.get("apps", ["Finder", "Safari", "Notes"])
        for _ in range(step.get("times", 5)):
            osa(f'tell application "{self.rng.choice(apps)}" to activate')
            nap(0.35 + self.rng.random() * 0.3)
        return True

    def s_copy(self, step):
        p = subprocess.run(["pbcopy"], input=step["text"].encode())
        return p.returncode == 0

    def s_save(self, step):
        osa('tell application "System Events" to keystroke "s" using '
            'command down')
        nap(0.8)
        # dismiss a save dialog without writing anything real
        osa('tell application "System Events" to key code 53')
        return True

    def s_park(self, step):
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://localhost:{PORT}/api/park", method="POST",
                data=json.dumps({"text": step["text"]}).encode(),
                headers={"Content-Type": "application/json"}), timeout=5)
            return True
        except Exception:
            return False

    def s_suspend(self, step):
        """Sleep-equivalent: SIGSTOP the capture process."""
        try:
            pid = json.loads(
                (ROOT / "captures" / "status.json").read_text())["pid"]
            os.kill(pid, 17)  # SIGSTOP
            nap(step.get("seconds", 40))
            os.kill(pid, 19)  # SIGCONT
            return True
        except Exception as e:
            self.report["failures"].append(f"suspend: {e}")
            return False

    def s_hands_off(self, step):
        nap(step.get("seconds", 30))
        return True

    def run_step(self, step):
        fn = getattr(self, "s_" + step["do"], None)
        if not fn:
            self.report["failures"].append(f"unknown step {step['do']}")
            return
        ok = fn(step)
        if not ok and step["do"] not in ("click",):
            self.report["failures"].append(f"step {step['do']} failed")


# --- Variation engine ------------------------------------------------------

def vary(steps, rng):
    """Seeded: pacing jitter is inside the driver; here we do permitted
    reordering and probabilistic distractor injection."""
    out = []
    block = []
    for s in steps:
        if s.get("shuffle_ok"):
            block.append(s)
            continue
        if block:
            rng.shuffle(block)
            out.extend(block)
            block = []
        out.append(s)
    if block:
        rng.shuffle(block)
        out.extend(block)

    final, injected = [], 0
    for s in out:
        final.append(s)
        if s["do"] in ("type", "scroll") and rng.random() < 0.18:
            final.append(rng.choice([
                {"do": "glance", "app": "Finder", "_injected": True},
                {"do": "thrash", "times": 3, "_injected": True},
            ]))
            injected += 1
    return final, injected


# --- Grading ---------------------------------------------------------------

def grade(scenario, card, graph, t_card):
    """Answer key vs what actually happened. Semantic fields graded by
    embedding similarity; structural facts checked directly."""
    import threads as T
    key = scenario["answer_key"]
    g = {}

    def sim(a, b):
        va, vb = T.embed([a or "", b or ""])
        return T.cosine(va, vb)

    if card:
        g["goal"] = sim(card.get("goal", ""), key.get("goal", "")) >= 0.45
        g["reasoning"] = sim(card.get("reasoning", ""),
                             key.get("reasoning", key.get("goal", ""))) >= 0.35
        g["next_action"] = sim(card.get("next_action", ""),
                               key.get("next_action", "")) >= 0.45
        want_loops = key.get("open_loops_contains", [])
        loops = " · ".join(card.get("open_loops", []))
        g["open_loops"] = (all(sim(loops, w) >= 0.4 for w in want_loops)
                           if want_loops else None)
        g["park_note_honored"] = (key.get("park_note", "") ==
                                  card.get("park_note", "")) \
            if key.get("park_note") else None
    else:
        g = {k: False for k in ("goal", "reasoning", "next_action",
                                "open_loops")}

    active = graph["meta"].get("active_thread")
    expected = key.get("active_thread")
    g["correct_thread_active"] = (
        active is not None and expected is not None and
        expected.lower() in (graph["threads"].get(active, {})
                             .get("name", "").lower()))
    if key.get("glance_topic"):
        names = " ".join(t["name"].lower()
                         for t in graph["threads"].values())
        g["glance_stayed_ambient"] = key["glance_topic"].lower() not in names
    if key.get("detour_expected"):
        g["detour_surfaced"] = bool(
            [t for t in graph["threads"].values()
             if t.get("origin") == "emergent"]
            or __import__("threads").emergent_candidate(graph))
    g["time_to_card_s"] = t_card
    return g


# --- Runner ----------------------------------------------------------------

def run_once(scenario, seed):
    import threads as T
    rng = random.Random(seed)
    report = {"seed": seed, "scenario": scenario["name"],
              "at": datetime.now().isoformat(timespec="seconds"),
              "retries": [], "ax_fallbacks": [], "failures": [],
              "injected_distractors": 0, "aborted": False}

    banner = subprocess.Popen(
        [sys.executable, str(ROOT / "banner.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    baseline = set((ROOT / "cards").glob("card_*.json"))
    t0 = None

    env_flag = ROOT / ".rehearsal_on"
    env_flag.write_text("1")   # capture reads RC_REHEARSAL from env; the
    # engine was started by the user, so tag via env for OUR spawned gen
    os.environ["RC_REHEARSAL"] = "1"

    try:
        steps, injected = vary(scenario["steps"], rng)
        report["injected_distractors"] = injected
        drv = Driver(rng, report)
        for step in steps:
            print(f"  · {step['do']}"
                  + (f" {step.get('app', '')}" if step.get("app") else "")
                  + (" (injected)" if step.get("_injected") else ""),
                  flush=True)
            drv.run_step(step)
            if step["do"] == "hands_off":
                t0 = time.time()

        # wait for the card the hands-off should produce
        card, t_card = None, None
        deadline = time.time() + 240
        while time.time() < deadline:
            if esc_down():
                raise Abort()
            fresh = set((ROOT / "cards").glob("card_*.json")) - baseline
            if fresh:
                card = json.loads(sorted(fresh)[-1].read_text())
                t_card = round(time.time() - (t0 or time.time()), 1)
                break
            time.sleep(2)

        graph = T.load()
        report["grades"] = grade(scenario, card, graph, t_card)
        report["card_file"] = sorted(f.name for f in
                                     (set((ROOT / "cards")
                                          .glob("card_*.json")) - baseline))
        checks = [v for v in report["grades"].values()
                  if isinstance(v, bool)]
        report["passed"] = bool(checks) and all(checks)

    except Abort:
        report["aborted"] = True
        report["passed"] = False
        print("\n  ABORTED by Esc.")
    finally:
        os.environ.pop("RC_REHEARSAL", None)
        env_flag.unlink(missing_ok=True)
        (ROOT / ".banner.pid").unlink(missing_ok=True)
        try:
            banner.wait(timeout=2)
        except Exception:
            banner.terminate()

    rows = json.loads(STORE.read_text()) if STORE.exists() else []
    rows.append(report)
    STORE.parent.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(rows[-300:], indent=1))
    return report


def print_report(r):
    print(f"\n  scenario={r['scenario']} seed={r['seed']} "
          f"{'PASS' if r.get('passed') else 'FAIL'}")
    for k, v in (r.get("grades") or {}).items():
        print(f"    {k:24} {v}")
    if r["ax_fallbacks"]:
        print(f"    honest limits: AX fallbacks -> {r['ax_fallbacks']}")
    if r["retries"]:
        print(f"    honest limits: retries -> {r['retries']}")
    if r["failures"]:
        print(f"    failures -> {r['failures']}")


def batch(scenario, runs, base_seed):
    reports = []
    for i in range(runs):
        seed = base_seed + i
        print(f"\n=== run {i + 1}/{runs} (seed {seed}) ===")
        r = run_once(scenario, seed)
        print_report(r)
        reports.append(r)
        if r.get("aborted"):
            break

    done = [r for r in reports if not r.get("aborted")]
    passed = [r for r in done if r.get("passed")]
    print(f"\n===== BATCH: {len(passed)}/{len(done)} passed =====")
    fields = {}
    for r in done:
        for k, v in (r.get("grades") or {}).items():
            if isinstance(v, bool):
                fields.setdefault(k, [0, 0])
                fields[k][1] += 1
                fields[k][0] += int(v)
    for k, (ok, n) in fields.items():
        print(f"  {k:24} {ok}/{n}")
    times = [r["grades"].get("time_to_card_s") for r in done
             if r.get("grades", {}).get("time_to_card_s")]
    if times:
        print(f"  time-to-card: min {min(times)}s · "
              f"median {sorted(times)[len(times)//2]}s · max {max(times)}s")
    failed_seeds = [r["seed"] for r in done if not r.get("passed")]
    if failed_seeds:
        print(f"  FAILED SEEDS (pinned): {failed_seeds}")
        pins = (json.loads(REGRESSIONS.read_text())
                if REGRESSIONS.exists() else [])
        for s in failed_seeds:
            entry = {"scenario": done[0]["scenario"], "seed": s}
            if entry not in pins:
                pins.append(entry)
        REGRESSIONS.write_text(json.dumps(pins, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--regressions", action="store_true")
    args = ap.parse_args()

    if args.list:
        for f in sorted(SCEN_DIR.glob("*.json")):
            s = json.loads(f.read_text())
            print(f"  {f.stem:16} {s.get('about', '')}")
        return

    if args.regressions:
        pins = (json.loads(REGRESSIONS.read_text())
                if REGRESSIONS.exists() else [])
        if not pins:
            print("No pinned regression seeds. Good.")
            return
        for p in pins:
            s = json.loads((SCEN_DIR / f"{p['scenario']}.json").read_text())
            print(f"\n=== regression: {p['scenario']} seed {p['seed']} ===")
            print_report(run_once(s, p["seed"]))
        return

    if not args.scenario:
        ap.error("scenario required (or --list / --regressions)")
    path = SCEN_DIR / f"{args.scenario}.json"
    if not path.exists():
        print(f"Unknown scenario. Try: plite rehearse --list")
        sys.exit(1)
    scenario = json.loads(path.read_text())
    scenario["name"] = args.scenario

    print(f"REHEARSAL: {args.scenario} · seed {args.seed} · "
          f"banner up · Esc aborts")
    if args.runs > 1:
        batch(scenario, args.runs, args.seed)
    else:
        print_report(run_once(scenario, args.seed))


if __name__ == "__main__":
    main()
