"""
validator.py -- STAGE 10a. Deterministic checks on the generated draft.

These run before the LLM critic, because they are free and they catch the
worst failures with certainty. A regex cannot be talked out of its opinion.

Design rule: FAIL CLOSED. Anything that fails here does not get "fixed and
sent" -- it goes to a human. We allow exactly one regeneration attempt, then
stop.
"""
import re
from typing import Dict, List, Any

BANNED = [
    r"\bguarantee(?:d|s)?\b", r"\bdefinitely\b", r"\bcertainly will\b",
    r"\bexclusiv(?:e|ity)\b", r"\blegally\b", r"\bexcited\b",
    r"\bon behalf of\b", r"\bI hope this (?:email )?finds you\b",
    r"\bcircle back\b.*\bsynerg", r"\breach out\b.*\bshortly\b",
]
MONEY = re.compile(
    r"(?:[$₹£€]|USD|INR|Rs\.?)\s?[\d,]+|\b\d+(?:\.\d+)?\s?"
    r"(?:x|%|k|m|mn|cr|crore|lakh|lakhs|million|billion)\b", re.I)
DATE_LIKE = re.compile(
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|"
    r"\b\d{1,2}\s?(?::|\.)\s?\d{2}\s?(?:am|pm)?\b|\b\d{1,2}\s?(?:am|pm)\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|"
    r"\b(?:tomorrow|next week|this week)\b", re.I)
URL = re.compile(r"https?://[^\s>）)\]]+")
PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\[(?:insert|name|company|date)\]|XXXX", re.I)
SCHEDULING_CLAIM = re.compile(
    r"\b(?:invite|invitation|calendar|booked|scheduled|put (?:it )?in|"
    r"blocked|hold(?:ing)? (?:the )?time)\b", re.I)

URL_ALLOWLIST = ["meet.google.com", "calendar.google.com", "cicero"]


def validate(draft: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    ctx expects:
      event            CalendarEvent or None
      allowed_facts    str  (the approved text the model was given)
      questions        list of questions they asked
      sender_first     str  our signer's first name
      max_words        int
    """
    v: List[str] = []
    d = draft or ""

    if not d.strip():
        return {"pass": False, "violations": ["empty draft"], "checks": 0}

    # ---- 1. length
    words = len(d.split())
    if words > ctx.get("max_words", 120):
        v.append(f"too long: {words} words")
    if words < 8:
        v.append("suspiciously short")

    # ---- 2. banned language
    for pat in BANNED:
        if re.search(pat, d, re.I):
            v.append(f"banned phrase matching /{pat}/")

    # ---- 3. no numbers about money, ever
    m = MONEY.search(d)
    if m:
        v.append(f"monetary or numeric claim: '{m.group(0)}'")

    # ---- 4. THE BIG ONE: no date or time that is not a real booked event.
    #        This is what stops the system promising a call that doesn't exist.
    dates = DATE_LIKE.findall(d)
    if dates:
        if not ctx.get("event"):
            v.append(f"mentions a time ({dates[:3]}) but no calendar event exists")
        else:
            ht = ctx["event"].human_time.lower()
            for token in set(x.lower() for x in dates):
                if token and token not in ht:
                    v.append(f"time reference '{token}' is not in the booked slot "
                             f"'{ctx['event'].human_time}'")

    # ---- 5. claims a booking with no event id behind it
    if SCHEDULING_CLAIM.search(d) and not ctx.get("event"):
        v.append("claims a call is set up, but no calendar event was created")

    # ---- 6. links must be ours
    for u in URL.findall(d):
        if not any(a in u for a in URL_ALLOWLIST):
            v.append(f"link to a non-allowlisted domain: {u}")

    # ---- 7. unresolved template variables
    if PLACEHOLDER.search(d):
        v.append("unfilled placeholder left in the text")

    # ---- 8. every question we HAD AN ANSWER FOR must be acknowledged.
    #        Questions with no approved answer are the router's problem, not
    #        this check's -- holding twice for the same reason is just noise.
    for q in ctx.get("answered_questions", []):
        words_q = [w for w in re.findall(r"[a-z]{5,}", q.lower())
                   if w not in {"would", "could", "there", "about", "which", "these"}]
        if words_q and not any(w in d.lower() for w in words_q[:4]):
            v.append(f"does not address their question: '{q[:60]}'")

    # ---- 9. signature present
    if ctx.get("sender_first") and ctx["sender_first"].lower() not in d.lower():
        v.append("missing sign-off")

    # ---- 10. promises we cannot keep
    if re.search(r"\bI(?:'ll| will) send\b|\bsending (?:you )?(?:over|across)\b", d, re.I):
        if not ctx.get("sendable_assets"):
            v.append("promises to send a document, but no assets are approved for sending")

    return {"pass": len(v) == 0, "violations": v, "checks": 10}
