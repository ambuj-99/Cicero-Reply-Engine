"""
gmail_io.py -- STAGES 1 and 11. Getting mail in, and getting replies out.

Two things here are easy to get wrong and both are visible to the recipient:

1. THREADING. A reply must carry In-Reply-To and References pointing at their
   message, and reuse the same threadId. Get this wrong and your reply appears
   as a brand new email, which instantly reads as automation.

2. THE SENDING ADDRESS. We must send from the mailbox that originally contacted
   them, using a Gmail "send-as" alias. If Cicero has separate credentials per
   outbound mailbox, swap in the right service object instead.
"""
import os, re, base64, json, email
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, make_msgid
from datetime import datetime, timezone
from typing import List
from .models import RawMessage

QUOTE_MARKERS = [
    r"\nOn .{5,120}wrote:", r"\n-{2,}\s*Forwarded message", r"\n-{2,}\s*Original Message",
    r"\n_{10,}", r"\nFrom:\s.+\nSent:\s", r"\n>{1,}\s",
]


def strip_quotes(body: str) -> str:
    """Keep only what they actually typed this time."""
    cut = len(body)
    for pat in QUOTE_MARKERS:
        m = re.search(pat, body)
        if m:
            cut = min(cut, m.start())
    return body[:cut].strip()


# =============================================================== MOCK
class MockGmail:
    """Reads .json fixtures from disk. Same interface as the real client, so
    the pipeline never knows the difference."""
    def __init__(self, fixture_dir="data/fixtures"):
        self.dir = fixture_dir
        self.sent = []

    def fetch_unread(self, limit=100) -> List[RawMessage]:
        out = []
        for fn in sorted(os.listdir(self.dir)):
            if not fn.endswith(".json"):
                continue
            d = json.load(open(os.path.join(self.dir, fn)))
            raw = d.get("body", "")
            out.append(RawMessage(
                message_id=d["headers"].get("Message-ID", f"<{fn}>"),
                gmail_id=fn,
                headers=d["headers"],
                subject=d["headers"].get("Subject", ""),
                body=strip_quotes(raw),
                raw_body=raw,
                received_at=datetime.fromisoformat(d.get("received_at")),
                to_recipients=d.get("to", []),
                cc_recipients=d.get("cc", []),
                has_attachment=d.get("has_attachment", False),
                has_calendar_part=d.get("has_calendar_part", False)))
        return out[:limit]

    def send_reply(self, from_mailbox, to_email, subject, body,
                   in_reply_to=None, references=None, thread_id=None):
        rec = {"from": from_mailbox, "to": to_email, "subject": subject,
               "body": body, "in_reply_to": in_reply_to}
        self.sent.append(rec)
        return {"id": "mock_sent_" + str(len(self.sent))}


# =============================================================== REAL
class Gmail:
    def __init__(self, token_path="token_gmail.json", user_id="me"):
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        self.svc = build("gmail", "v1",
                         credentials=Credentials.from_authorized_user_file(token_path),
                         cache_discovery=False)
        self.user_id = user_id

    def fetch_unread(self, limit=50, label="INBOX", query="is:unread") -> List[RawMessage]:
        res = self.svc.users().messages().list(
            userId=self.user_id, q=query, labelIds=[label],
            maxResults=limit).execute()
        out = []
        for ref in res.get("messages", []):
            m = self.svc.users().messages().get(
                userId=self.user_id, id=ref["id"], format="raw").execute()
            raw = base64.urlsafe_b64decode(m["raw"])
            msg = email.message_from_bytes(raw)
            headers = {k: v for k, v in msg.items()}

            body, has_att, has_cal = "", False, False
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    disp = str(part.get("Content-Disposition") or "")
                    if ct == "text/calendar":
                        has_cal = True
                    if "attachment" in disp:
                        has_att = True
                    if ct == "text/plain" and "attachment" not in disp and not body:
                        body = part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", "replace")
            else:
                body = msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or "utf-8", "replace")

            try:
                received = parsedate_to_datetime(headers.get("Date"))
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
            except Exception:
                received = datetime.now(timezone.utc)

            out.append(RawMessage(
                message_id=headers.get("Message-ID", f"<gmail-{ref['id']}>"),
                gmail_id=ref["id"], headers=headers,
                subject=headers.get("Subject", ""),
                body=strip_quotes(body), raw_body=body, received_at=received,
                to_recipients=re.findall(r"[\w\.\-\+]+@[\w\.\-]+", headers.get("To", "")),
                cc_recipients=re.findall(r"[\w\.\-\+]+@[\w\.\-]+", headers.get("Cc", "")),
                has_attachment=has_att, has_calendar_part=has_cal))
        return out

    def send_reply(self, from_mailbox, to_email, subject, body,
                   in_reply_to=None, references=None, thread_id=None):
        mime = MIMEText(body, "plain", "utf-8")
        mime["To"] = to_email
        mime["From"] = from_mailbox            # must be a verified send-as alias
        mime["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = (references or "") + " " + in_reply_to
        mime["Message-ID"] = make_msgid(domain=from_mailbox.split("@")[-1])
        payload = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if thread_id:
            payload["threadId"] = thread_id
        return self.svc.users().messages().send(userId=self.user_id, body=payload).execute()

    def mark_read(self, gmail_id):
        self.svc.users().messages().modify(
            userId=self.user_id, id=gmail_id,
            body={"removeLabelIds": ["UNREAD"]}).execute()


def get_gmail(mock: bool):
    if mock or not os.path.exists("token_gmail.json"):
        return MockGmail()
    return Gmail()
