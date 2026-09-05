"""
tests/test_core.py -- run with:  python -m pytest tests/ -v

Two things are tested here and nothing else, deliberately:

  1. pick_slot   -- the only place a meeting time is chosen. It is a pure
                    function, so it can be tested exhaustively with no
                    network, no clock and no calendar.
  2. the router  -- the only place a send/hold decision is made.

If these two are right, the worst failures are impossible. Everything else
degrades gracefully.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import yaml

from src.scheduler import pick_slot, resolve_timezone, human_time
from src.router import Router
from src.models import Extraction, Identity, Thread, RawMessage
from src.validator import validate
from src.facts import FactBook

CFG = yaml.safe_load(open("config/policy.yaml"))["scheduling"]
IST = ZoneInfo("Asia/Kolkata")


def _now(y=2026, mo=9, d=7, h=9):
    return datetime(y, mo, d, h, 0, tzinfo=IST)      # a Monday


# ============================================================ slot picker
def test_respects_minimum_lead_time():
    """Nobody wants an invite for a call in twenty minutes."""
    slot = pick_slot(_now(), [], CFG, "Asia/Kolkata")
    assert slot >= _now() + timedelta(hours=CFG["min_lead_hours"])


def test_never_books_outside_business_hours():
    slot = pick_slot(_now(), [], CFG, "Asia/Kolkata")
    assert CFG["business_hours"]["start"] <= slot.hour < CFG["business_hours"]["end"]


def test_never_books_on_a_weekend():
    for day in range(1, 15):
        slot = pick_slot(_now(d=day), [], CFG, "Asia/Kolkata")
        assert slot is None or slot.weekday() < 5


def test_never_books_on_a_configured_holiday():
    cfg = dict(CFG, holidays=["2026-09-08", "2026-09-09"])
    slot = pick_slot(_now(), [], cfg, "Asia/Kolkata")
    assert slot.date().isoformat() not in cfg["holidays"]


def test_avoids_busy_blocks_including_buffers():
    """A back-to-back booking is a double-booking in practice."""
    busy = [(datetime(2026, 9, 8, 10, 0, tzinfo=IST),
             datetime(2026, 9, 8, 17, 30, tzinfo=IST))]
    slot = pick_slot(_now(), busy, CFG, "Asia/Kolkata")
    buf = timedelta(minutes=CFG["buffer_minutes"])
    for b0, b1 in busy:
        assert not (slot - buf < b1 and slot + timedelta(minutes=30) + buf > b0)


def test_honours_a_stated_day():
    slot = pick_slot(_now(), [], CFG, "Asia/Kolkata", "Thursday works for me")
    assert slot.weekday() == 3


def test_honours_a_stated_part_of_day():
    slot = pick_slot(_now(), [], CFG, "Asia/Kolkata", "Tuesday afternoon")
    assert slot.weekday() == 1 and slot.hour >= 13


def test_books_in_the_recipients_timezone_not_ours():
    slot = pick_slot(_now(), [], CFG, "America/New_York")
    assert 10 <= slot.astimezone(ZoneInfo("America/New_York")).hour < 18


def test_returns_none_when_the_calendar_is_full():
    busy = [(datetime(2026, 9, 7, 0, 0, tzinfo=IST),
             datetime(2026, 12, 31, 0, 0, tzinfo=IST))]
    assert pick_slot(_now(), busy, CFG, "Asia/Kolkata") is None


def test_is_deterministic():
    """Same inputs, same output. This is why the LLM is not allowed near it."""
    a = pick_slot(_now(), [], CFG, "Asia/Kolkata", "Wednesday")
    b = pick_slot(_now(), [], CFG, "Asia/Kolkata", "Wednesday")
    assert a == b


# ============================================================ timezone
def test_contact_record_beats_everything():
    tz, c = resolve_timezone("PST", "x@example.com", "Asia/Kolkata", "Europe/London", "UTC")
    assert tz == "Asia/Kolkata" and c == 1.0


def test_unknown_timezone_returns_low_confidence():
    """Low confidence must be below the router floor, so it holds rather
    than booking someone a 3am call."""
    tz, c = resolve_timezone("", "x@example.xyz", "", "", "Asia/Kolkata")
    assert c < CFG.get("min_conf", 0.8)


# ============================================================ router
def _msg(body="hi", to=None, cc=None, att=False):
    return RawMessage(message_id="<m>", gmail_id="g", headers={}, subject="s",
                      body=body, raw_body=body, received_at=datetime.now(IST),
                      to_recipients=to or ["deals@cicero.com"], cc_recipients=cc or [],
                      has_attachment=att)


def _id(conf=0.95):
    return Identity("a@b.com", "A", "out@cicero.com", "tk", None, [], conf, "test")


def _th(state="NEW", turns=0):
    return Thread("tk", "a@b.com", "out@cicero.com", state=state, machine_turns=turns)


R = Router()


def test_money_in_their_message_always_stops():
    ex = Extraction(action_intent="BOOK_NOW", confidence=0.99)
    d = R.decide(_msg("we want 3 crore for the business"), _id(), _th(), ex, 1.0, 1.0)
    assert d.route == "HUMAN_ONLY" and d.gate == "Gate 2"


def test_sensitive_keyword_always_stops():
    ex = Extraction(action_intent="BOOK_NOW", confidence=0.99)
    d = R.decide(_msg("send the NDA first"), _id(), _th(), ex, 1.0, 1.0)
    assert d.route == "HUMAN_ONLY"


def test_attachment_always_stops():
    ex = Extraction(action_intent="DECLINE", confidence=0.99)
    d = R.decide(_msg("no thanks", att=True), _id(), _th(), ex, 1.0, 1.0)
    assert d.route == "HUMAN_ONLY"


def test_three_parties_stops():
    ex = Extraction(action_intent="BOOK_NOW", confidence=0.99)
    d = R.decide(_msg("ok", cc=["x@y.com", "z@y.com"]), _id(), _th(), ex, 1.0, 1.0)
    assert d.route == "HUMAN_ONLY"


def test_two_machine_turns_stops():
    ex = Extraction(action_intent="DECLINE", confidence=0.99)
    d = R.decide(_msg("no"), _id(), _th(turns=2), ex, 1.0, 1.0)
    assert d.route == "HUMAN_ONLY" and d.gate == "Gate 1"


def test_low_identity_never_auto_sends():
    """We must never reply in the wrong voice to the wrong person."""
    ex = Extraction(action_intent="DECLINE", confidence=0.99)
    d = R.decide(_msg("no thanks"), _id(conf=0.4), _th(), ex, 1.0, 1.0)
    assert d.route != "AUTO_SEND" and d.binding_constraint == "identity"


def test_partial_fact_coverage_never_auto_sends():
    ex = Extraction(action_intent="DEFER", confidence=0.99,
                    open_questions=["what multiple", "who are you"])
    d = R.decide(_msg("later, but who are you"), _id(), _th(), ex, 0.5, 1.0)
    assert d.route == "DRAFT_FOR_REVIEW" and d.binding_constraint == "fact_coverage"


def test_allowlisted_decline_auto_sends():
    ex = Extraction(action_intent="DECLINE", confidence=0.99)
    d = R.decide(_msg("not interested"), _id(), _th(), ex, 1.0, 1.0)
    assert d.route == "AUTO_SEND"


def test_book_now_on_a_cold_thread_is_reviewed_not_sent():
    """Booking is allowed; sending the email about it unsupervised is not,
    until the allowlist is widened on evidence."""
    ex = Extraction(action_intent="BOOK_NOW", confidence=0.99)
    d = R.decide(_msg("let's talk"), _id(), _th(state="NEW"), ex, 1.0, 1.0)
    assert d.route == "DRAFT_FOR_REVIEW"


def test_book_now_after_we_offered_auto_sends():
    ex = Extraction(action_intent="BOOK_NOW", confidence=0.99)
    d = R.decide(_msg("yes please"), _id(), _th(state="CALL_OFFERED"), ex, 1.0, 1.0)
    assert d.route == "AUTO_SEND"


# ============================================================ scheduling trigger
def test_enthusiasm_alone_does_not_book():
    ex = Extraction(action_intent="WANTS_INFO", confidence=0.9, sentiment="POSITIVE")
    assert R.should_schedule(ex, _th(state="NEW")) is False


def test_yes_after_an_offer_books():
    ex = Extraction(action_intent="WANTS_INFO", confidence=0.9)
    assert R.should_schedule(ex, _th(state="CALL_OFFERED")) is True


def test_sensitive_flag_blocks_booking():
    ex = Extraction(action_intent="BOOK_NOW", confidence=0.99, sensitive_flags=["PRICING"])
    assert R.should_schedule(ex, _th()) is False


# ============================================================ validator
class _Ev:
    human_time = "Tuesday 08 Sep, 3:00pm-3:30pm IST"


def test_rejects_a_time_with_no_event():
    r = validate("Priya - see you Tuesday at 3pm.\n\nArjun",
                 {"event": None, "sender_first": "Arjun", "answered_questions": []})
    assert not r["pass"]


def test_rejects_a_time_that_is_not_the_booked_one():
    r = validate("Priya - Thursday 3:00pm works, invite sent.\n\nArjun",
                 {"event": _Ev(), "sender_first": "Arjun", "answered_questions": []})
    assert not r["pass"]


def test_accepts_the_correct_booked_time():
    r = validate("Priya - I have put thirty minutes in for Tuesday 08 Sep, "
                 "3:00pm-3:30pm IST. Move it if that clashes.\n\nArjun",
                 {"event": _Ev(), "sender_first": "Arjun", "answered_questions": []})
    assert r["pass"], r["violations"]


def test_rejects_money():
    r = validate("Priya - we typically pay around 4 crore.\n\nArjun",
                 {"event": None, "sender_first": "Arjun", "answered_questions": []})
    assert not r["pass"]


def test_rejects_claiming_a_booking_with_no_event():
    r = validate("Priya - the invite is in your calendar.\n\nArjun",
                 {"event": None, "sender_first": "Arjun", "answered_questions": []})
    assert not r["pass"]


def test_rejects_promising_a_document():
    r = validate("Priya - I will send our deck across.\n\nArjun",
                 {"event": None, "sender_first": "Arjun", "answered_questions": [],
                  "sendable_assets": []})
    assert not r["pass"]


# ============================================================ facts
def test_unknown_question_drops_coverage():
    fb = FactBook()
    score, ans, unans = fb.coverage(["who are you", "what did you pay for the last one"])
    assert score < 1.0 and len(unans) == 1


def test_no_questions_is_full_coverage():
    assert FactBook().coverage([])[0] == 1.0


# ============================================================ redaction
from src.redact import Redactor

def test_redacts_indian_phone_formats():
    for s in ["+91 98765 43210", "9876543210", "098765-43210"]:
        r = Redactor()
        assert "[PHONE_1]" in r.redact(f"call me on {s}")

def test_redacts_money_but_not_plain_counts():
    r = Redactor()
    out = r.redact("we did 4.2 crore with 12 people")
    assert "[MONEY_1]" in out and "12 people" in out

def test_redacts_tax_ids():
    r = Redactor()
    out = r.redact("GSTIN 27AAPFU0939F1ZV and PAN ABCDE1234F")
    assert "[GSTIN_1]" in out and "[PAN_1]" in out

def test_names_and_companies_survive():
    """Tone depends on them. This is a deliberate trade-off."""
    r = Redactor()
    out = r.redact("Priya Nair, Kesar Foods, call +91 98765 43210")
    assert "Priya Nair" in out and "Kesar Foods" in out and "[PHONE_1]" in out

def test_rehydrate_round_trips():
    r = Redactor()
    r.redact("call +91 98765 43210")
    assert r.rehydrate("I will call [PHONE_1]") == "I will call +91 98765 43210"

def test_leak_detection():
    r = Redactor()
    assert r.leaked("ring you on [PHONE_1] tomorrow")
    assert not r.leaked("ring you tomorrow")

def test_repeated_value_reuses_one_token():
    r = Redactor()
    out = r.redact("9876543210 ... again 9876543210")
    assert out.count("[PHONE_1]") == 2 and "[PHONE_2]" not in out
