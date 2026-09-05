"""
identity.py -- STAGE 2. The hardest and most valuable part of the system.

The problem: every reply is FORWARDED into one primary inbox. That means
the `From:` header is often our own forwarding mailbox, not the founder.
And we must reply FROM the mailbox that originally contacted them, or the
thread breaks in their email client and we look automated.

We resolve in order of reliability and stop at the first hit. Each method
carries a confidence score, because the router needs to know how sure we are.
"""
import re
from typing import Optional, Dict, List
from .models import RawMessage, Identity

EMAIL_RE = re.compile(r"[\w\.\-\+']+@[\w\.\-]+\.\w+")

# The block Gmail inserts when it forwards. Different locales phrase it
# differently, hence the alternation.
FWD_BLOCK = re.compile(
    r"-{2,}\s*(?:Forwarded message|Original Message)\s*-{2,}(.{0,600})",
    re.S | re.I)
FWD_FROM = re.compile(r"^\s*From:\s*(.+)$", re.M | re.I)


def _parse_addr(s: str):
    """'Priya Nair <priya@kesarfoods.in>' -> ('Priya Nair', 'priya@kesarfoods.in')"""
    if not s:
        return "", ""
    m = EMAIL_RE.search(s)
    email = m.group(0).lower() if m else ""
    name = re.sub(r"<.*?>", "", s).replace('"', "").strip(" ,;")
    return name, email


def _norm_subject(subject: str) -> str:
    """Strip Re:/Fwd: so we can match threads when headers were stripped."""
    s = subject or ""
    for _ in range(6):
        s = re.sub(r"^\s*(re|fw|fwd|aw|sv)\s*:\s*", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def resolve_identity(msg: RawMessage, store, our_domains: List[str]) -> Identity:
    h = {k.lower(): v for k, v in msg.headers.items()}
    notes: List[str] = []

    in_reply_to = h.get("in-reply-to")
    refs = [r for r in re.findall(r"<[^>]+>", h.get("references", ""))]

    header_from_name, header_from = _parse_addr(h.get("from", ""))
    is_internal_from = any(header_from.endswith("@" + d) for d in our_domains)

    real_sender, sender_name, method, conf = "", "", "", 0.0
    outbound_mailbox = None

    # -- Method 1: X-Forwarded-For. Gmail filters set this. Most reliable.
    xff = h.get("x-forwarded-for") or h.get("x-original-sender")
    if xff:
        # format is often "<forwarded-to> <original-sender>" or just the sender
        addrs = EMAIL_RE.findall(xff)
        cand = [a.lower() for a in addrs if not any(a.lower().endswith("@" + d) for d in our_domains)]
        if cand:
            real_sender, method, conf = cand[0], "x-forwarded-for", 0.95
            notes.append("recovered sender from X-Forwarded-For")

    # -- Method 2: Reply-To, when it disagrees with From
    if not real_sender:
        rt_name, rt = _parse_addr(h.get("reply-to", ""))
        if rt and not any(rt.endswith("@" + d) for d in our_domains):
            real_sender, sender_name, method, conf = rt, rt_name, "reply-to", 0.90
            notes.append("recovered sender from Reply-To")

    # -- Method 3: From is external. The simple, common case.
    if not real_sender and header_from and not is_internal_from:
        real_sender, sender_name, method, conf = header_from, header_from_name, "from-header", 0.95

    # -- Method 4: parse the forwarded block out of the body. Last resort.
    if not real_sender:
        blk = FWD_BLOCK.search(msg.raw_body or "")
        if blk:
            fm = FWD_FROM.search(blk.group(1))
            if fm:
                n, e = _parse_addr(fm.group(1))
                if e and not any(e.endswith("@" + d) for d in our_domains):
                    real_sender, sender_name, method, conf = e, n, "body-forward-block", 0.55
                    notes.append("sender parsed out of the forwarded body -- LOW confidence")

    if not real_sender:
        real_sender, method, conf = header_from or "unknown@unknown", "unresolved", 0.20
        notes.append("could not resolve a real sender")

    # ---------- which of OUR mailboxes owns this thread? ----------
    # Priority 1: thread lineage. If References/In-Reply-To point at a message
    # we sent, we know the mailbox with certainty.
    thread_key = None
    for ref in ([in_reply_to] if in_reply_to else []) + refs:
        row = store.conn.execute(
            "SELECT thread_key, outbound_mailbox FROM threads WHERE thread_key=?",
            (ref,)).fetchone()
        if row:
            thread_key, outbound_mailbox = row["thread_key"], row["outbound_mailbox"]
            conf = max(conf, 0.95)
            notes.append("thread matched on References header")
            break

    # Priority 2: the contact record tells us which mailbox we used.
    contact = store.get_contact(real_sender)
    if not outbound_mailbox and contact:
        outbound_mailbox = contact.outbound_mailbox
        notes.append("mailbox taken from contact record")

    # Priority 3: any To/Cc address of ours on the message.
    if not outbound_mailbox:
        for a in msg.to_recipients + msg.cc_recipients:
            if any(a.lower().endswith("@" + d) for d in our_domains):
                outbound_mailbox = a.lower()
                notes.append("mailbox guessed from To/Cc -- verify")
                conf = min(conf, 0.70)
                break

    # ---------- thread key ----------
    # We use OUR OWN key, never Gmail's, because Gmail thread ids do not
    # survive forwarding and we want the key stable across mailboxes.
    if not thread_key:
        thread_key = f"{real_sender}|{_norm_subject(msg.subject)}"
        # if a thread with this key exists, this is a continuation
        if not store.get_thread(thread_key):
            notes.append("new thread key created from sender+subject")

    # ---------- confidence penalties ----------
    if not outbound_mailbox:
        conf = min(conf, 0.55)
        notes.append("no owning mailbox found -- cannot reply in the right voice")

    # role addresses are read by an unknown number of people
    local = real_sender.split("@")[0]
    if local in {"info", "contact", "admin", "sales", "office", "enquiry", "hello", "team"}:
        conf = min(conf, 0.50)
        notes.append("role address -- unknown number of readers")

    # a Cicero teammate forwarded this by hand, with a note on top
    if is_internal_from and not xff and method == "body-forward-block":
        notes.append("hand-forwarded by a teammate -- a human has already seen this")

    if contact and not sender_name:
        sender_name = contact.name

    return Identity(real_sender=real_sender, sender_name=sender_name or real_sender.split("@")[0],
                    outbound_mailbox=outbound_mailbox, thread_key=thread_key,
                    in_reply_to=in_reply_to, references=refs,
                    confidence=round(conf, 2), method=method, notes=notes)
