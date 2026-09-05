"""
store.py -- all persistence, in one SQLite file. No ORM, no server.

Why SQLite: it is a single file on disk, ships with Python, and gives you
transactions and unique constraints. Those constraints are what stop the
system double-sending or double-booking. A JSON file cannot do that.
"""
import sqlite3, json, csv, os
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from .models import Contact, Thread, Commitment

DB_PATH = os.environ.get("CICERO_DB", "data/store.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  email TEXT PRIMARY KEY, name TEXT, org TEXT, sender_type TEXT,
  timezone TEXT, campaign TEXT, outbound_mailbox TEXT, status TEXT,
  suppressed_until TEXT, channel_preference TEXT, aliases TEXT
);
CREATE TABLE IF NOT EXISTS threads (
  thread_key TEXT PRIMARY KEY, contact_email TEXT, outbound_mailbox TEXT,
  state TEXT, machine_turns INTEGER, last_human_touch TEXT,
  calendar_event_id TEXT, reply_count INTEGER
);
CREATE TABLE IF NOT EXISTS commitments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_key TEXT, text TEXT, kind TEXT,
  created_by TEXT, created_at TEXT, due_at TEXT, discharged_at TEXT
);
-- UNIQUE is the dedupe guarantee: the same email cannot be processed twice.
CREATE TABLE IF NOT EXISTS processed (
  message_id TEXT PRIMARY KEY, processed_at TEXT, route TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_key TEXT, to_email TEXT,
  from_mailbox TEXT, subject TEXT, body TEXT, in_reply_to TEXT,
  references_hdr TEXT, send_after TEXT, status TEXT, created_at TEXT,
  cancelled_reason TEXT
);
CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE, thread_key TEXT,
  payload TEXT, gate TEXT, binding_constraint TEXT, status TEXT,
  created_at TEXT, resolved_at TEXT, human_edit TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, message_id TEXT,
  stage TEXT, detail TEXT
);
-- one active calendar event per thread, enforced by the database itself
CREATE TABLE IF NOT EXISTS events (
  thread_key TEXT PRIMARY KEY, event_id TEXT, start_iso TEXT,
  status TEXT, created_at TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = None):
        self.path = path or DB_PATH
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -------------------------------------------------- contacts
    def load_contacts_csv(self, path="config/contacts.csv"):
        with open(path) as f:
            for row in csv.DictReader(f):
                self.conn.execute("""INSERT OR REPLACE INTO contacts
                    (email,name,org,sender_type,timezone,campaign,outbound_mailbox,
                     status,suppressed_until,channel_preference,aliases)
                    VALUES (?,?,?,?,?,?,?,?,NULL,'','[]')""",
                    (row["email"].lower(), row["name"], row["org"], row["sender_type"],
                     row["timezone"], row["campaign"], row["outbound_mailbox"], row["status"]))
        self.conn.commit()

    def get_contact(self, email: str) -> Optional[Contact]:
        email = (email or "").lower().strip()
        r = self.conn.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
        if not r:
            # check alias lists -- people reply from a second address all the time
            for row in self.conn.execute("SELECT * FROM contacts"):
                if email in json.loads(row["aliases"] or "[]"):
                    r = row
                    break
        if not r:
            return None
        return Contact(email=r["email"], name=r["name"], org=r["org"],
                       sender_type=r["sender_type"], timezone=r["timezone"],
                       campaign=r["campaign"], outbound_mailbox=r["outbound_mailbox"],
                       status=r["status"], suppressed_until=r["suppressed_until"],
                       channel_preference=r["channel_preference"] or "",
                       aliases=json.loads(r["aliases"] or "[]"))

    def upsert_contact(self, c: Contact):
        self.conn.execute("""INSERT OR REPLACE INTO contacts
            (email,name,org,sender_type,timezone,campaign,outbound_mailbox,status,
             suppressed_until,channel_preference,aliases) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (c.email.lower(), c.name, c.org, c.sender_type, c.timezone, c.campaign,
             c.outbound_mailbox, c.status, c.suppressed_until, c.channel_preference,
             json.dumps(c.aliases)))
        self.conn.commit()

    def add_alias(self, canonical_email: str, alias: str):
        c = self.get_contact(canonical_email)
        if c and alias.lower() not in c.aliases:
            c.aliases.append(alias.lower())
            self.upsert_contact(c)

    def suppress(self, email: str, days: Optional[int], reason: str):
        """days=None means permanent. This is stage 5 and it runs FIRST."""
        until = None if days is None else (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        status = "SUPPRESSED"
        self.conn.execute("UPDATE contacts SET status=?, suppressed_until=? WHERE email=?",
                          (status, until, email.lower()))
        self.log("-", "SUPPRESS", f"{email} for {days} days :: {reason}")
        self.conn.commit()

    # -------------------------------------------------- threads
    def get_thread(self, key: str) -> Optional[Thread]:
        r = self.conn.execute("SELECT * FROM threads WHERE thread_key=?", (key,)).fetchone()
        if not r:
            return None
        return Thread(thread_key=r["thread_key"], contact_email=r["contact_email"],
                      outbound_mailbox=r["outbound_mailbox"], state=r["state"],
                      machine_turns=r["machine_turns"], last_human_touch=r["last_human_touch"],
                      calendar_event_id=r["calendar_event_id"], reply_count=r["reply_count"])

    def upsert_thread(self, t: Thread):
        self.conn.execute("""INSERT OR REPLACE INTO threads
            (thread_key,contact_email,outbound_mailbox,state,machine_turns,
             last_human_touch,calendar_event_id,reply_count) VALUES (?,?,?,?,?,?,?,?)""",
            (t.thread_key, t.contact_email, t.outbound_mailbox, t.state, t.machine_turns,
             t.last_human_touch, t.calendar_event_id, t.reply_count))
        self.conn.commit()

    # -------------------------------------------------- dedupe
    def already_processed(self, message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM processed WHERE message_id=?",
                                 (message_id,)).fetchone() is not None

    def mark_processed(self, message_id: str, route: str):
        self.conn.execute("INSERT OR IGNORE INTO processed VALUES (?,?,?)",
                          (message_id, now_iso(), route))
        self.conn.commit()

    # -------------------------------------------------- commitments
    def add_commitment(self, c: Commitment):
        self.conn.execute("""INSERT INTO commitments
            (thread_key,text,kind,created_by,created_at,due_at,discharged_at)
            VALUES (?,?,?,?,?,?,NULL)""",
            (c.thread_key, c.text, c.kind, c.created_by, now_iso(), c.due_at))
        self.conn.commit()

    def open_commitments(self, thread_key: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT text FROM commitments WHERE thread_key=? AND discharged_at IS NULL",
            (thread_key,)).fetchall()
        return [r["text"] for r in rows]

    # -------------------------------------------------- events
    def get_event(self, thread_key: str):
        return self.conn.execute(
            "SELECT * FROM events WHERE thread_key=? AND status='ACTIVE'",
            (thread_key,)).fetchone()

    def record_event(self, thread_key, event_id, start_iso):
        self.conn.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                          (thread_key, event_id, start_iso, "ACTIVE", now_iso()))
        self.conn.commit()

    def cancel_event_record(self, thread_key):
        self.conn.execute("UPDATE events SET status='CANCELLED' WHERE thread_key=?", (thread_key,))
        self.conn.commit()

    # -------------------------------------------------- outbox / review
    def enqueue_send(self, **kw):
        self.conn.execute("""INSERT INTO outbox
            (thread_key,to_email,from_mailbox,subject,body,in_reply_to,references_hdr,
             send_after,status,created_at,cancelled_reason)
            VALUES (?,?,?,?,?,?,?,?,'PENDING',?,NULL)""",
            (kw["thread_key"], kw["to_email"], kw["from_mailbox"], kw["subject"],
             kw["body"], kw.get("in_reply_to"), kw.get("references_hdr"),
             kw["send_after"], now_iso()))
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]

    def due_sends(self):
        return self.conn.execute(
            "SELECT * FROM outbox WHERE status='PENDING' AND send_after<=?",
            (now_iso(),)).fetchall()

    def mark_sent(self, oid, status="SENT", reason=None):
        self.conn.execute("UPDATE outbox SET status=?, cancelled_reason=? WHERE id=?",
                          (status, reason, oid))
        self.conn.commit()

    def enqueue_review(self, message_id, thread_key, payload, gate, binding):
        self.conn.execute("""INSERT OR REPLACE INTO review_queue
            (message_id,thread_key,payload,gate,binding_constraint,status,created_at,
             resolved_at,human_edit) VALUES (?,?,?,?,?,'PENDING',?,NULL,NULL)""",
            (message_id, thread_key, json.dumps(payload), gate, binding, now_iso()))
        self.conn.commit()

    def pending_reviews(self):
        return self.conn.execute(
            "SELECT * FROM review_queue WHERE status='PENDING' ORDER BY created_at").fetchall()

    def resolve_review(self, rid, status, human_edit=None):
        self.conn.execute(
            "UPDATE review_queue SET status=?, resolved_at=?, human_edit=? WHERE id=?",
            (status, now_iso(), human_edit, rid))
        self.conn.commit()

    # -------------------------------------------------- audit
    def log(self, message_id, stage, detail):
        self.conn.execute("INSERT INTO audit (ts,message_id,stage,detail) VALUES (?,?,?,?)",
                          (now_iso(), message_id, stage, str(detail)[:2000]))
        self.conn.commit()
