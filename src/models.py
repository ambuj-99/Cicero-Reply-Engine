"""
models.py -- the shapes of data that move between stages.

Nothing here does any work. These are just typed containers so that when
stage 6 hands something to stage 7, you know exactly what fields exist.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------- inbound
@dataclass
class RawMessage:
    """Straight off Gmail, before we've understood anything."""
    message_id: str                 # RFC Message-ID header, our dedupe key
    gmail_id: str                   # Gmail's own id
    headers: Dict[str, str]
    subject: str
    body: str                       # plain text, quoted history stripped
    raw_body: str                   # everything, quotes included
    received_at: datetime
    to_recipients: List[str] = field(default_factory=list)
    cc_recipients: List[str] = field(default_factory=list)
    has_attachment: bool = False
    has_calendar_part: bool = False


@dataclass
class Identity:
    """Who actually wrote this, and which of our mailboxes owns the thread."""
    real_sender: str
    sender_name: str
    outbound_mailbox: Optional[str]
    thread_key: str
    in_reply_to: Optional[str]
    references: List[str]
    confidence: float               # 0.0 - 1.0
    method: str                     # how we worked it out, for the audit log
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- memory
@dataclass
class Contact:
    email: str
    name: str = ""
    org: str = ""
    sender_type: str = "UNKNOWN"    # FOUNDER | BROKER | UNKNOWN
    timezone: str = ""
    campaign: str = ""
    outbound_mailbox: str = ""
    status: str = "ACTIVE"          # ACTIVE | SUPPRESSED | UNDELIVERABLE | STALE
    suppressed_until: Optional[str] = None
    channel_preference: str = ""    # "" | phone | video
    aliases: List[str] = field(default_factory=list)


@dataclass
class Thread:
    thread_key: str
    contact_email: str
    outbound_mailbox: str
    state: str = "NEW"              # NEW|ENGAGED|CALL_OFFERED|CALL_BOOKED|RESCHEDULING|CLOSED
    machine_turns: int = 0          # consecutive bot sends with no human touch
    last_human_touch: Optional[str] = None
    calendar_event_id: Optional[str] = None
    reply_count: int = 0


@dataclass
class Commitment:
    """Something we promised. Prevents the system contradicting itself."""
    thread_key: str
    text: str
    kind: str                       # SEND_DOC | FOLLOW_UP | ANSWER_LATER | INTRO
    created_by: str                 # MACHINE | HUMAN
    due_at: Optional[str] = None
    discharged_at: Optional[str] = None


# ---------------------------------------------------------------- LLM out
@dataclass
class Extraction:
    """What the LLM read out of the message. Facts only, no decisions."""
    action_intent: str              # drives the route
    secondary_intents: List[str] = field(default_factory=list)
    sender_type_guess: str = "UNKNOWN"
    open_questions: List[str] = field(default_factory=list)
    defer_subtype: str = "UNKNOWN"
    reengage_hint: str = ""         # free text like "after March 31"
    stated_availability: str = ""
    timezone_hint: str = ""
    channel_preference: str = ""
    referral_name: str = ""
    referral_email: str = ""
    deals: List[Dict[str, Any]] = field(default_factory=list)
    sensitive_flags: List[str] = field(default_factory=list)
    sentiment: str = "NEUTRAL"
    confidence: float = 0.0
    reasoning: str = ""


# ---------------------------------------------------------------- decision
@dataclass
class ConfidenceVector:
    identity: float = 1.0
    intent: float = 1.0
    fact_coverage: float = 1.0
    schedule: float = 1.0
    state: float = 1.0

    def minimum(self):
        pairs = [("identity", self.identity), ("intent", self.intent),
                 ("fact_coverage", self.fact_coverage), ("schedule", self.schedule),
                 ("state", self.state)]
        name, val = min(pairs, key=lambda p: p[1])
        return name, val


@dataclass
class Decision:
    route: str                      # AUTO_SEND | DRAFT_FOR_REVIEW | HUMAN_ONLY | NO_ACTION
    reasons: List[str] = field(default_factory=list)
    binding_constraint: str = ""
    gate: str = ""                  # which human gate fired, if any
    confidence: ConfidenceVector = field(default_factory=ConfidenceVector)


@dataclass
class CalendarEvent:
    event_id: str
    start_iso: str
    end_iso: str
    human_time: str                 # "Tuesday 09 Sep, 3:00-3:30pm IST"
    meet_link: str
    timezone: str


@dataclass
class Outcome:
    """Everything that happened to one message. This is the audit record."""
    message_id: str
    identity: Optional[Identity] = None
    extraction: Optional[Extraction] = None
    decision: Optional[Decision] = None
    event: Optional[CalendarEvent] = None
    draft: str = ""
    validation: Dict[str, Any] = field(default_factory=dict)
    action: str = ""
    error: str = ""

    def to_dict(self):
        return {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
                for k, v in asdict(self).items()}
