#!/usr/bin/env python3
"""Recovery Card - chat awareness.

Chat surfaces are where intent gets written down in the user's own words,
and where an AI restates it back. Those are opposite things, and this
module exists to never confuse them.

The rules, in order of authority:

  COMPOSITION IS AUTHORSHIP. Text captured while the user is actively
  typing into a recognized chat input is user-authored intent - the
  thread's candidate return-point, authority just below a park note.

  ATTRIBUTION NEEDS STRUCTURE. Chat text may only be quoted as "you said"
  if it was composition-captured or structurally attributed via the
  accessibility tree. Anything else - scraped, stale, ambiguous - is scene
  context for classification only, and is never quoted.

  THE AI'S RESTATEMENT IS NOT THE PERSON'S WORDS. The 12B is explicitly
  instructed to separate the person's messages from the AI's by layout,
  and never to treat the AI's rephrasing of a request as the user's own.

Visible-surface capture only: no chat app's internal storage is ever read.
All of it local, git-ignored, and excludable via the dashboard's "Chat
awareness" switch. (Consented chat-history connectors are future work in
SPEC.md - the real-Phossil path to full logs.)
"""

import re

# --- Surface recognition ----------------------------------------------------
# Small per-surface adapter rules. Kept deliberately simple: app names,
# URL patterns, and pane hints from window titles.

_URL_CHAT = re.compile(
    r"(claude\.ai|chatgpt\.com|chat\.openai\.com|gemini\.google\.com"
    r"|perplexity\.ai|copilot\.microsoft\.com|poe\.com)", re.I)

_APP_CHAT = {"Claude", "ChatGPT"}

# Cursor's agent/chat pane: the app is an IDE, but the focused element is a
# conversational composer. Title/focus hints identify it.
_CURSOR_PANE = re.compile(r"(chat|composer|agent)", re.I)


def surface(meta):
    """Which chat surface this frame is on, or None.

    Returns {"kind": ..., "via": app|url|pane} - the adapter that matched.
    """
    app = meta.get("app", "")
    ax = meta.get("ax") or {}
    url = ax.get("url", "")

    if app in _APP_CHAT:
        return {"kind": app.lower() + "-desktop", "via": "app"}
    if url and _URL_CHAT.search(url):
        return {"kind": _URL_CHAT.search(url).group(1).lower(), "via": "url"}
    if app == "Cursor":
        hint = (ax.get("focused") or "") + " " + (ax.get("selected") or "")
        if _CURSOR_PANE.search(hint):
            return {"kind": "cursor-pane", "via": "pane"}
    return None


def is_composing(meta):
    """Actively typing into a recognized chat surface, with a live draft."""
    if not surface(meta):
        return False
    eng = meta.get("engagement") or {}
    return eng.get("doing") == "typing" and bool(composed_text(meta))


def composed_text(meta):
    """The draft the user is writing, from the focused element's AX value."""
    ax = meta.get("ax") or {}
    txt = (ax.get("composed") or "").strip()
    return txt if len(txt) >= 8 else ""  # a fragment is not intent


# --- Temporal stitching -----------------------------------------------------

def _norm(s):
    return " ".join(s.split())


def stitch(frame_metas):
    """Merge chat text across the rolling frame window by overlap.

    Successive frames of the same conversation mostly repeat each other
    with a little new text at the edge. Overlap-deduplicating them
    reconstructs a wider slice than any single frame holds. Every piece
    keeps its capture timestamp; the result is SCENE CONTEXT only - it is
    never quoted as the user's words (see attribution rules above).
    """
    pieces = []
    for m in frame_metas:
        if not surface(m):
            continue
        ax = m.get("ax") or {}
        for key in ("selected", "composed"):
            t = _norm(ax.get(key) or "")
            if len(t) >= 20:
                pieces.append({"at": m.get("timestamp", ""), "text": t,
                               "attributed": key == "composed"})
    if not pieces:
        return []

    merged = []
    for p in pieces:
        placed = False
        for m in merged:
            a, b = m["text"], p["text"]
            if b in a:
                placed = True
                break
            if a in b:
                m["text"], m["at"] = b, p["at"]
                m["attributed"] = m["attributed"] and p["attributed"]
                placed = True
                break
            # suffix-of-a overlaps prefix-of-b (the scroll case): find
            # b's opening in a, then verify the whole tail matches.
            probe = b[:40]
            idx = a.find(probe)
            if idx >= 0 and b.startswith(a[idx:]):
                m["text"] = a[:idx] + b
                m["at"] = p["at"]
                m["attributed"] = False  # merged text loses authorship
                placed = True
                break
        if not placed:
            merged.append(dict(p))
    return merged[-6:]


# --- Agent-working detection ------------------------------------------------

def agent_working(meta, screen_changed):
    """Input idle + screen still changing on a chat/agent surface.

    That is the signature of the user's agent continuing without them. The
    card gets to report it: "While you were away, your agent...".
    """
    if not surface(meta):
        return False
    eng = meta.get("engagement") or {}
    idle = eng.get("doing") in ("idle", "mousing")
    return bool(idle and screen_changed)


# --- Prompt fragments (the 12B's standing orders for chat windows) ----------

CHAT_PROMPT_RULES = (
    "Some frames show a chat with an AI assistant. Rules for those:\n"
    "- Use the visual layout to tell the person's messages from the AI's "
    "(user messages are typically right-aligned or visually distinct).\n"
    "- NEVER treat the AI's restatement or rephrasing of a request as the "
    "person's own words.\n"
    "- Only text explicitly marked USER-AUTHORED below may be quoted as "
    "what the person said or wanted. Everything else from chat windows is "
    "background scene context only.")
