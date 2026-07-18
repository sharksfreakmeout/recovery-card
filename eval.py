#!/usr/bin/env python3
"""Recovery Card - Evaluation.

Park notes are the user's own words about what they were doing, written
before they stepped away. That makes them ground truth, recorded before
the model ever saw the screenshots. Any card generated while a park note
was active can therefore be scored honestly: did the card match what the
person actually said they were doing?

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
        if c.get("park_note", "").strip():
            out.append((p, c))
    return out


def tally(results):
    judged = [r for r in results.values() if r.get("verdict") in
              ("correct", "incorrect")]
    correct = sum(1 for r in judged if r["verdict"] == "correct")
    return correct, len(judged)


def print_tally(results, prefix="Running tally"):
    correct, total = tally(results)
    if total == 0:
        print(f"{DIM}{prefix}: nothing judged yet{OFF}")
        return
    pct = 100 * correct / total
    colour = GREEN if pct >= 70 else (YELLOW if pct >= 50 else RED)
    print(f"{BOLD}{prefix}: {colour}{correct}/{total}{OFF}{BOLD} "
          f"({pct:.0f}%){OFF}")


def show_pair(path, card, n, of):
    print()
    print("=" * 70)
    print(f"{DIM}Card {n} of {of}   {path.name}   "
          f"{card.get('generated_at', '')}{OFF}")
    print("=" * 70)
    print()
    print(f"{BOLD}GROUND TRUTH — what you said before stepping away:{OFF}")
    print(f'  {GREEN}"{card["park_note"]}"{OFF}')
    print()
    print(f"{BOLD}WHAT THE CARD SAID:{OFF}")
    print(f"  goal        {card.get('goal', '')}")
    print(f"  next step   {card.get('next_action', '')}")
    loops = card.get("open_loops") or []
    if loops:
        print(f"  open loops  {'; '.join(str(x) for x in loops)}")
    print()
    print(f"{DIM}  confidence: {card.get('confidence', '?')}"
          f"   model: {card.get('model', '?')}"
          + ("   [REDUCED MODEL]" if card.get("reduced_model") else "")
          + ("   [FAIL-CLOSED]" if card.get("fail_closed") else "")
          + f"{OFF}")
    print(f"{DIM}  evidence:   {card.get('evidence', '')}{OFF}")
    print()


def judge(results, cards):
    print()
    print(f"For each card: does it match what you actually said you were "
          f"doing?")
    print(f"{DIM}  c = correct    i = incorrect    s = skip    q = quit "
          f"and save{OFF}")

    for n, (path, card) in enumerate(cards, 1):
        show_pair(path, card, n, len(cards))

        while True:
            try:
                answer = input("  correct? [c/i/s/q] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                save_results(results)
                print_tally(results, "Final tally")
                return

            if answer in ("c", "i"):
                results[path.name] = {
                    "verdict": "correct" if answer == "c" else "incorrect",
                    "judged_at": datetime.now().isoformat(timespec="seconds"),
                    "park_note": card["park_note"],
                    "goal": card.get("goal", ""),
                    "confidence": card.get("confidence", ""),
                    "model": card.get("model", ""),
                    "fail_closed": bool(card.get("fail_closed")),
                }
                save_results(results)
                print_tally(results)
                break
            if answer == "s":
                break
            if answer == "q":
                save_results(results)
                print()
                print_tally(results, "Final tally")
                return
            print(f"{DIM}  please type c, i, s or q{OFF}")

    print()
    print_tally(results, "Final tally")


def breakdown(results):
    """A little extra honesty: how the model does when it claims confidence."""
    judged = [r for r in results.values()
              if r.get("verdict") in ("correct", "incorrect")]
    if not judged:
        return
    print()
    print(f"{BOLD}Breakdown{OFF}")
    for level in ("high", "medium", "low"):
        rows = [r for r in judged if r.get("confidence") == level]
        if not rows:
            continue
        ok = sum(1 for r in rows if r["verdict"] == "correct")
        print(f"  confidence {level:<7} {ok}/{len(rows)}")
    failed = [r for r in judged if r.get("fail_closed")]
    if failed:
        print(f"  {DIM}(of these, {len(failed)} were fail-closed cards, which "
              f"admit they have nothing rather than guessing){OFF}")


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
        print("  1. Open the app and type a line in the Park it box, e.g.")
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
