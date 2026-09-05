"""
prefilter.py -- STAGE 3. Cheap rules that run BEFORE any LLM call.

Roughly a third of what lands in a reply inbox is not a reply at all.
Sending any of it to a language model wastes money and, worse, invites
the model to invent an intent for a bounce message.

Every check here is a regex or a header lookup. Zero cost, zero ambiguity.
"""
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop (?:contacting|emailing)|"
    r"do not (?:contact|email)|opt.?out|मुझे हटा)\b", re.I)

LEGAL_THREAT = re.compile(
    r"\b(cease and desist|legal action|report(?:ing)? you|spam(?:ming)? complaint|"
    r"gdpr|data protection authority|harassment)\b", re.I)

DEPARTED = re.compile(
    r"\b(no longer (?:with|at)|has left the company|is no longer employed|"
    r"i have left)\b", re.I)

OOO_BODY = re.compile(
    r"\b(out of (?:the )?office|on (?:annual |maternity |paternity )?leave|"
    r"away from my desk|on vacation|currently travell?ing|auto[- ]?reply)\b", re.I)

BOUNCE_SENDERS = re.compile(r"(mailer-daemon|postmaster|no-?reply)@", re.I)

RETURN_DATE = re.compile(
    r"\b(?:back|return(?:ing)?|available again)\s+(?:on\s+)?"
    r"(\d{1,2}\s+\w+|\w+\s+\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)", re.I)


def prefilter(msg, identity) -> Optional[Tuple[str, str, dict]]:
    """
    Returns None if this is a genuine human reply that should continue
    down the pipeline. Otherwise returns (verdict, reason, extras).
    """
    h = {k.lower(): v for k, v in msg.headers.items()}
    body = msg.body or ""

    # ---- 1. auto-generated mail, identified by header, not by guesswork
    if h.get("auto-submitted", "").lower().startswith("auto"):
        return _ooo_or_bounce(msg, body)
    if h.get("x-autoreply") or h.get("x-autorespond"):
        return _ooo_or_bounce(msg, body)
    if h.get("precedence", "").lower() in {"bulk", "auto_reply", "junk", "list"}:
        return ("NON_REPLY", "bulk or list mail", {})
    if h.get("return-path", "").strip() in {"<>", ""} and h.get("from", ""):
        if BOUNCE_SENDERS.search(h.get("from", "")):
            return ("BOUNCE", "null return-path from a daemon", {})
    if BOUNCE_SENDERS.search(h.get("from", "")):
        return ("BOUNCE", "message from a mail daemon", {})

    # ---- 2. read receipts: log as engagement, never reply
    if h.get("content-type", "").startswith("multipart/report") or \
       "disposition-notification" in " ".join(h.keys()).lower():
        return ("NON_REPLY", "read receipt", {})

    # ---- 3. an inbound calendar invite. NEVER counter-invite.
    if msg.has_calendar_part:
        return ("INBOUND_INVITE", "they sent us an invite -- accept, do not reply", {})

    # ---- 4. opt-out. Global suppression, and we send NOTHING back.
    if OPT_OUT.search(body):
        return ("OPT_OUT", "explicit opt-out language", {})

    # ---- 5. legal or abuse threat. Freeze and page a human immediately.
    if LEGAL_THREAT.search(body):
        return ("LEGAL_FREEZE", "legal or abuse language detected", {})

    # ---- 6. nothing but quoted history -- an accidental send
    stripped = re.sub(r"^\s*>.*$", "", body, flags=re.M).strip()
    if len(stripped) < 3 and len(msg.raw_body or "") > 200:
        return ("NON_REPLY", "no new text, only quoted history", {})

    # ---- 7. body is empty and there is an attachment: a document drop
    if not stripped and msg.has_attachment:
        return ("HUMAN_ONLY", "attachment with no message body", {})

    # ---- 8. stale forward
    age = (datetime.now(timezone.utc) - msg.received_at).days
    if age > 5:
        return ("HUMAN_ONLY", f"message is {age} days old", {})

    return None


def _ooo_or_bounce(msg, body):
    """An auto-reply is not one thing. Three sub-cases, three actions."""
    if DEPARTED.search(body):
        return ("CONTACT_DEPARTED", "sender has left the company", {})
    m = RETURN_DATE.search(body)
    if m:
        return ("OOO", "out of office with a return date", {"return_hint": m.group(1)})
    if OOO_BODY.search(body):
        return ("OOO", "out of office", {})
    return ("NON_REPLY", "auto-generated mail", {})
