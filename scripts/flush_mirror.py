#!/usr/bin/env python3
"""One-time mirror-poison flush.

Marks existing cards whose text cites PLite's own surfaces (or that were
smoke-test artifacts) as contaminated, removes nodes derived solely from
them, and heals every thread's return-point to the latest clean card or
user statement. Idempotent; prints what it did.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import card as card_mod          # noqa: E402  (MIRROR_RE lives there)
import threads as T              # noqa: E402


def main():
    contaminated = set()
    marked = 0
    for p in sorted((ROOT / "cards").glob("card_*.json")):
        c = json.loads(p.read_text())
        text = " ".join(str(c.get(k, "")) for k in
                        ("evidence", "goal", "reasoning", "next_action"))
        smoke = (c.get("park_note") or "").startswith("smoke test:")
        if c.get("contaminated") or card_mod.MIRROR_RE.search(text) or smoke:
            if not c.get("contaminated"):
                c["contaminated"] = True
                p.write_text(json.dumps(c, indent=2))
                marked += 1
            contaminated.add(p.name)

    g = T.load()

    # Nodes whose every named-card source is contaminated: gone. Sources
    # that are just "card" (pre-provenance) or "user" stay conservative:
    # user-added nodes are never removed here.
    removed = []
    keep = []
    for n in g["nodes"]:
        srcs = n.get("sources") or []
        named = [s for s in srcs if s.startswith("card_")]
        if named and all(s in contaminated for s in named) \
                and "user" not in srcs:
            removed.append(n["label"])
        else:
            keep.append(n)
    g["nodes"] = keep

    # Heal every return-point: latest clean card for the thread, else the
    # latest user statement (park/composed/resume) in its history.
    healed = []
    clean_cards = []
    for p in sorted((ROOT / "cards").glob("card_*.json"), reverse=True):
        if p.name in contaminated:
            continue
        try:
            clean_cards.append(json.loads(p.read_text()))
        except Exception:
            pass

    for tid, t in g["threads"].items():
        rp = t.get("return_point", "")
        if rp and not card_mod.MIRROR_RE.search(rp) \
                and "capture-to-card" not in rp.lower():
            continue  # already clean
        new_rp = ""
        for c in clean_cards:
            if c.get("thread") == t["name"] and c.get("next_action"):
                new_rp = c["next_action"]
                break
        if not new_rp:
            for h in reversed(t.get("history", [])):
                if h["kind"] in ("park", "composed", "resume") and h["text"]:
                    new_rp = h["text"]
                    break
        if new_rp != rp:
            t["return_point"] = new_rp.replace("`", "")
            healed.append(f'{t["name"]}: "{rp[:40]}" -> "{new_rp[:40]}"')

    T.save(g)
    print(f"cards marked contaminated: {marked} "
          f"(total contaminated: {len(contaminated)})")
    print(f"nodes removed: {removed or 'none'}")
    print(f"return-points healed: {len(healed)}")
    for h in healed:
        print(f"  {h}")


if __name__ == "__main__":
    main()
