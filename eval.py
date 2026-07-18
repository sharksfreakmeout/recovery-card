#!/usr/bin/env python3
"""Recovery Card - Evaluation.

Park notes are the user's own words about what they were doing, written
before they stepped away. That makes them ground truth, recorded before
the model ever saw the screenshots. Any card generated while a park note
was active can therefore be scored honestly.

Scoring is per field, not per card. A card can nail the goal and miss the
next action entirely, and a single correct/incorrect verdict throws that
information away. Four marks per card: goal, reasoning, next_action,
open_loops.

Run:  python3 eval.py           score cards not yet judged
      python3 eval.py --all     re-judge everything from scratch
      python3 eval.py --tally   just print the score, judge nothing
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CARDS = ROOT / "cards"
EVAL = ROOT / "eval"
RESULTS = EVAL / "results.json"

FIELDS = ["goal", "reasoning", "next_action", "open_loops",
          "right_thread"]

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
OFF = "\033[0m"


def load_results():
    if RESULTS.exists():
        try:
            return json.loads(RESULTS.read_text())
        except Exception:
            pass
    return {}


def save_results(results):
    EVAL.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2))


def scored_cards():
    """Cards that carry a park note, oldest first.

    A card without a park note has no ground truth to check it against, so
    it is not scoreable. That is a deliberate limit: we would rather
    measure a smaller number honestly than grade the model on its own
    output.
    """
    out = []
    for p in sorted(CARDS.glob("card_*.json")):
        try:
            c = json.loads(p.read_text())
        except Exception:
            continue
        if c.get("rehearsal"):
            continue  # fence: rehearsal data never mixes into human tallies
        if c.get("contaminated"):
            continue  # mirror poison never enters the tally
        if c.get("park_note", "").strip():
            out.append((p, c))
    return out


def pending(results):
    """Scoreable cards that have not been judged yet."""
    return [(p, c) for p, c in scored_cards() if p.name not in results]


def tally_dict(results):
    """The tally as plain data, for the app's scoring panel.

    app.py imports this so the UI and the command line can never disagree
    about the score.
    """
    per, correct, judged = tally(results)

    # Lab trials and in-product taps are one dataset, reported together and
    # also split, so it stays visible how much of the score came from real
    # use versus deliberate scoring sessions.
    by_source = {}
    for src in ("lab", "product"):
        subset = {k: v for k, v in results.items()
                  if src in (v.get("sources") or ["lab"])}
        _, c, j = tally(subset)
        by_source[src] = {"correct": c, "judged": j}

    return {
        "fields": {f: {"correct": per[f][0], "judged": per[f][1]}
                   for f in FIELDS},
        "overall": {"correct": correct, "judged": judged},
        "percent": round(100 * correct / judged, 1) if judged else None,
        "by_source": by_source,
        "corrections": sum(len(v.get("corrections") or [])
                           for v in results.values()),
    }


def upsert(results, card_file, card, marks, source="product",
           correction=None, replace=False):
    """Merge marks into a card's record. Shared by the CLI and the app.

    Two kinds of verdict land in the same store:
      - "lab"     a deliberate scoring pass against a park note
      - "product" a tap on the live card while actually using it

    They merge rather than overwrite, because a person taps one field now
    and another later. Passing replace=True (the lab panel, which submits
    all four fields at once) overwrites instead.
    """
    rec = results.get(card_file) or {}
    existing = {} if replace else (rec.get("marks") or {})

    for f, v in (marks or {}).items():
        if f in FIELDS:
            existing[f] = v if v in (True, False) else None

    sources = set(rec.get("sources") or [])
    sources.add(source)

    rec.update({
        "marks": existing,
        "sources": sorted(sources),
        "judged_at": datetime.now().isoformat(timespec="seconds"),
        "park_note": card.get("park_note", ""),
        "goal": card.get("goal", ""),
        "confidence": card.get("confidence", ""),
        "model": card.get("model", ""),
        "trigger": card.get("trigger", ""),
        "fail_closed": bool(card.get("fail_closed")),
    })

    if correction:
        rec.setdefault("corrections", []).append(correction)

    results[card_file] = rec
    save_results(results)
    return results


def record(results, card_file, card, marks):
    """Lab-trial scoring: all four fields submitted together."""
    return upsert(results, card_file, card, marks,
                  source="lab", replace=True)


def tally(results):
    """Per-field hits and totals, plus the overall roll-up.

    Fields marked not-applicable are excluded from their denominator
    rather than counted as either right or wrong.
    """
    per = {f: [0, 0] for f in FIELDS}  # [correct, judged]
    for r in results.values():
        for f, mark in (r.get("marks") or {}).items():
            if f not in per or mark not in (True, False):
                continue
            per[f][1] += 1
            if mark:
                per[f][0] += 1
    total_correct = sum(v[0] for v in per.values())
    total_judged = sum(v[1] for v in per.values())
    return per, total_correct, total_judged


def print_tally(results, prefix="Running tally"):
    per, correct, judged = tally(results)
    if judged == 0:
        print(f"{DIM}{prefix}: nothing judged yet{OFF}")
        return

    parts = []
    for f in FIELDS:
        ok, n = per[f]
        if n == 0:
            continue
        pct = 100 * ok / n
        colour = GREEN if pct >= 70 else (YELLOW if pct >= 50 else RED)
        parts.append(f"{f} {colour}{ok}/{n}{OFF}{BOLD}")

    pct = 100 * correct / judged
    colour = GREEN if pct >= 70 else (YELLOW if pct >= 50 else RED)
    print(f"{BOLD}{prefix}: " + ", ".join(parts) +
          f", overall {colour}{correct}/{judged}{OFF}{BOLD} ({pct:.0f}%){OFF}")


def show_pair(path, card, n, of):
    print()
    print("=" * 72)
    print(f"{DIM}Card {n} of {of}   {path.name}   "
          f"{card.get('generated_at', '')}"
          f"   trigger: {card.get('trigger', '?')}{OFF}")
    print("=" * 72)
    print()
    print(f"{BOLD}GROUND TRUTH — what you said before stepping away:{OFF}")
    print(f'  {GREEN}"{card["park_note"]}"{OFF}')
    print()
    print(f"{BOLD}WHAT THE CARD SAID:{OFF}")
    print(f"  {BOLD}goal{OFF}         {card.get('goal', '')}")
    print(f"  {BOLD}reasoning{OFF}    {card.get('reasoning', '')}")
    print(f"  {BOLD}next_action{OFF}  {card.get('next_action', '')}")
    loops = card.get("open_loops") or []
    if loops:
        print(f"  {BOLD}open_loops{OFF}   " +
              f"\n{' ' * 15}".join(str(x) for x in loops))
    else:
        print(f"  {BOLD}open_loops{OFF}   {DIM}(none){OFF}")
    print()
    print(f"{DIM}  confidence: {card.get('confidence', '?')}"
          f"   model: {card.get('model', '?')}"
          + ("   [REDUCED MODEL]" if card.get("reduced_model") else "")
          + ("   [FAIL-CLOSED]" if card.get("fail_closed") else "")
          + f"{OFF}")
    print(f"{DIM}  evidence:   {card.get('evidence', '')}{OFF}")
    print()


def ask_fields(card):
    """Four marks per card. Returns dict, or None to skip, 'quit' to stop."""
    marks = {}
    for f in FIELDS:
        while True:
            try:
                a = input(f"  {f} correct? [y/n/-/s/q] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return "quit"

            if a in ("y", "n"):
                marks[f] = (a == "y")
                break
            if a == "-":
                marks[f] = None  # not applicable, excluded from the denominator
                break
            if a == "s":
                return None
            if a == "q":
                return "quit"
            print(f"{DIM}    y = correct, n = wrong, - = not applicable, "
                  f"s = skip card, q = quit{OFF}")
    return marks


def judge(results, cards):
    print()
    print("For each card, mark each field against what you actually said.")
    print(f"{DIM}  y = correct   n = wrong   - = not applicable   "
          f"s = skip card   q = quit and save{OFF}")

    for n, (path, card) in enumerate(cards, 1):
        show_pair(path, card, n, len(cards))
        marks = ask_fields(card)

        if marks == "quit":
            save_results(results)
            print()
            print_tally(results, "Final tally")
            return
        if marks is None:
            continue

        results[path.name] = {
            "marks": marks,
            "judged_at": datetime.now().isoformat(timespec="seconds"),
            "park_note": card["park_note"],
            "goal": card.get("goal", ""),
            "confidence": card.get("confidence", ""),
            "model": card.get("model", ""),
            "trigger": card.get("trigger", ""),
            "fail_closed": bool(card.get("fail_closed")),
        }
        save_results(results)
        print()
        print_tally(results)

    print()
    print_tally(results, "Final tally")


def breakdown(results):
    """Where the model is overconfident, and how it does per field."""
    per, correct, judged = tally(results)
    if judged == 0:
        return

    print()
    print(f"{BOLD}By field{OFF}")
    for f in FIELDS:
        ok, n = per[f]
        if n:
            print(f"  {f:<12} {ok}/{n}")

    print()
    print(f"{BOLD}By claimed confidence{OFF}")
    for level in ("high", "medium", "low"):
        rows = [r for r in results.values() if r.get("confidence") == level]
        ok = sum(1 for r in rows for m in (r.get("marks") or {}).values()
                 if m is True)
        n = sum(1 for r in rows for m in (r.get("marks") or {}).values()
                if m in (True, False))
        if n:
            print(f"  {level:<12} {ok}/{n}")

    # Lab trials and in-product taps are one report, split for visibility.
    print()
    print(f"{BOLD}By source{OFF}")
    for src, label in (("lab", "lab trials"), ("product", "in product")):
        subset = {k: v for k, v in results.items()
                  if src in (v.get("sources") or ["lab"])}
        _, c, j = tally(subset)
        if j:
            print(f"  {label:<12} {c}/{j}")

    corrections = [(k, c) for k, v in results.items()
                   for c in (v.get("corrections") or [])]
    if corrections:
        print()
        print(f"{BOLD}Corrections you wrote{OFF}  "
              f"{DIM}(treated as truth on later cards){OFF}")
        for k, c in corrections[-5:]:
            print(f"  {DIM}{c.get('field', '?'):<12}{OFF} "
                  f"\"{c.get('text', '')}\"")

    failed = [r for r in results.values() if r.get("fail_closed")]
    if failed:
        print()
        print(f"  {DIM}{len(failed)} fail-closed card(s) included, which admit "
              f"they have nothing rather than guessing{OFF}")


def main():
    args = set(sys.argv[1:])
    results = load_results()

    if "--tally" in args:
        print_tally(results, "Score")
        breakdown(results)
        return

    cards = scored_cards()
    if not cards:
        print("No scoreable cards yet.")
        print()
        print("A card can only be scored if a park note was active when it "
              "was generated,")
        print("because the park note is the ground truth it gets checked "
              "against.")
        print()
        print("To create one:")
        print("  1. Type a line in the Park it box, e.g.")
        print('     "about to write the eval harness"')
        print("  2. Step away until the card generates.")
        print("  3. Run this again.")
        return

    if "--all" not in args:
        pending = [(p, c) for p, c in cards if p.name not in results]
        if not pending:
            print("Everything with ground truth has already been judged.")
            print_tally(results, "Score")
            breakdown(results)
            print()
            print(f"{DIM}Run  python3 eval.py --all  to re-judge from "
                  f"scratch.{OFF}")
            return
        cards = pending

    judge(results, cards)
    breakdown(results)


if __name__ == "__main__":
    main()
