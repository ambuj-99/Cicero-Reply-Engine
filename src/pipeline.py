"""
pipeline.py -- the orchestrator. Runs the eleven stages, in order, for one
message at a time.

Read this file top to bottom and you have read the whole system.
"""
import os, re, yaml, random, traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from .models import Outcome, Contact, Thread, Commitment, Extraction, Decision
from .store import Store, now_iso
from .identity import resolve_identity
from .prefilter import prefilter
from .facts import FactBook
from .router import Router
from . import llm, scheduler, validator
from .redact import Redactor
from .gmail_io import get_gmail


class Pipeline:
    def __init__(self, mock=True, our_domains=None, dry_run=True):
        self.mock = mock
        self.dry_run = dry_run
        self.policy = yaml.safe_load(open("config/policy.yaml"))
        self.voice = yaml.safe_load(open("config/brand_voice.yaml"))
        self.factbook = FactBook()
        self.router = Router()
        self.store = Store()
        self.gmail = get_gmail(mock)
        self.calendar = scheduler.get_calendar(mock)
        self.our_domains = our_domains or ["cicero-out1.com", "cicero-out2.com",
                                           "cicero-out3.com", "cicero.com"]

    # =================================================================
    def run_once(self, limit=100):
        outcomes = []
        for msg in self.gmail.fetch_unread(limit=limit):
            try:
                outcomes.append(self.process(msg))
            except Exception as e:
                self.store.log(msg.message_id, "CRASH", traceback.format_exc()[:1500])
                o = Outcome(message_id=msg.message_id, error=str(e), action="CRASHED_TO_HUMAN")
                self.store.enqueue_review(msg.message_id, "?", {"error": str(e)},
                                          "Gate 3", "exception")
                outcomes.append(o)
        return outcomes

    # =================================================================
    def process(self, msg) -> Outcome:
        o = Outcome(message_id=msg.message_id)
        S = self.store

        # ---------- STAGE 0: dedupe -------------------------------------
        if S.already_processed(msg.message_id):
            o.action = "SKIPPED_DUPLICATE"
            return o

        # ---------- STAGE 1-2: identity ---------------------------------
        ident = resolve_identity(msg, S, self.our_domains)
        o.identity = ident
        S.log(msg.message_id, "IDENTITY", f"{ident.real_sender} via {ident.method} "
                                          f"conf={ident.confidence} mailbox={ident.outbound_mailbox}")

        # ---------- STAGE 3: non-reply filter ---------------------------
        pf = prefilter(msg, ident)
        if pf:
            verdict, reason, extras = pf
            o.action = self._handle_non_reply(msg, ident, verdict, reason, extras)
            S.mark_processed(msg.message_id, o.action)
            return o

        # ---------- STAGE 4: load memory --------------------------------
        contact = S.get_contact(ident.real_sender)
        if not contact:
            contact = Contact(email=ident.real_sender, name=ident.sender_name,
                              sender_type="UNKNOWN",
                              outbound_mailbox=ident.outbound_mailbox or "")
            S.upsert_contact(contact)
        # a known person replying from a new address: remember the alias
        if contact.email != ident.real_sender:
            S.add_alias(contact.email, ident.real_sender)

        thread = S.get_thread(ident.thread_key)
        if not thread:
            thread = Thread(thread_key=ident.thread_key, contact_email=contact.email,
                            outbound_mailbox=ident.outbound_mailbox or contact.outbound_mailbox)
            S.upsert_thread(thread)
        state_conf = 1.0 if thread.outbound_mailbox else 0.6

        # ---------- STAGE 5: SUPPRESS THE SEQUENCER (before anything else)
        # This is the first write we make. A follow-up landing after they
        # replied is the most embarrassing failure this system can have,
        # and it must be prevented even if every later stage crashes.
        S.suppress(contact.email, days=14, reason="replied -- pausing sequence")
        S.log(msg.message_id, "SUPPRESS", "sequence paused on reply")

        # ---------- STAGE 5b: REDACT before anything leaves the machine ---
        # Nothing sensitive reaches the provider. Names and companies stay
        # (tone depends on them); numbers, IDs and contact details do not.
        red = Redactor()
        safe_body = red.redact(msg.body)
        if red.map:
            S.log(msg.message_id, "REDACT", red.summary())

        # ---------- STAGE 6: extract (LLM CALL 1) -----------------------
        ex = llm.extract(body=safe_body, subject=msg.subject,
                         sender_type_known=contact.sender_type,
                         thread_state=thread.state, thread_history="")
        # Our own records ALWAYS beat the model on sender type.
        sender_type = contact.sender_type if contact.sender_type != "UNKNOWN" \
            else ex.sender_type_guess
        o.extraction = ex
        S.log(msg.message_id, "EXTRACT", f"{ex.action_intent} conf={ex.confidence} "
                                         f"q={len(ex.open_questions)} flags={ex.sensitive_flags}")

        # ---------- STAGE 6b: fact coverage (deterministic) -------------
        coverage, answered, unanswered = self.factbook.coverage(ex.open_questions)
        if unanswered:
            S.log(msg.message_id, "FACTS", f"unanswerable: {unanswered}")

        # ---------- STAGE 8a: can we even schedule? ---------------------
        want_call = self.router.should_schedule(ex, thread)
        tz, tz_conf = scheduler.resolve_timezone(
            signature_text=msg.raw_body, email=ident.real_sender,
            contact_tz=contact.timezone, llm_hint=ex.timezone_hint,
            default_tz=self.policy["scheduling"]["default_timezone"])
        schedule_conf = tz_conf if want_call else 1.0

        # ---------- STAGE 7: ROUTE (deterministic) ----------------------
        dec = self.router.decide(msg, ident, thread, ex, coverage,
                                 schedule_conf, state_conf)
        o.decision = dec
        S.log(msg.message_id, "ROUTE", f"{dec.route} :: {dec.reasons} "
                                       f"binding={dec.binding_constraint} gate={dec.gate}")

        if dec.route == "NO_ACTION":
            o.action = "NO_ACTION"
            S.mark_processed(msg.message_id, o.action)
            return o

        if dec.route == "HUMAN_ONLY":
            S.enqueue_review(msg.message_id, thread.thread_key,
                             {"from": ident.real_sender, "org": contact.org,
                              "body": msg.body[:900], "intent": ex.action_intent,
                              "reasons": dec.reasons, "draft": None},
                             dec.gate, dec.binding_constraint)
            o.action = "QUEUED_NO_DRAFT"
            S.mark_processed(msg.message_id, o.action)
            return o

        # ---------- STAGE 8b: CREATE THE EVENT BEFORE WRITING THE EMAIL --
        event = None
        if want_call:
            event = self._schedule(msg, ident, contact, thread, ex, tz, o)
            if event is None and ex.action_intent == "BOOK_NOW":
                # We could not book. Do NOT write an email that implies we did.
                S.enqueue_review(msg.message_id, thread.thread_key,
                                 {"from": ident.real_sender, "body": msg.body[:900],
                                  "intent": ex.action_intent,
                                  "reasons": ["they asked for a call but no slot could be booked"],
                                  "draft": None}, "Gate 2", "schedule")
                o.action = "QUEUED_SCHEDULING_FAILED"
                S.mark_processed(msg.message_id, o.action)
                return o
        o.event = event

        # ---------- STAGE 9: DRAFT (LLM CALL 2) -------------------------
        signer = (ident.outbound_mailbox or "team@cicero.com").split("@")[0].split(".")[0].title()
        prompt = llm.build_draft_prompt(
            voice=self.voice, sender_type=sender_type,
            contact_name=contact.name or ident.sender_name, org=contact.org,
            sender_first_name=signer, intent=ex.action_intent,
            questions_with_answers=answered, event=event,
            ledger=S.open_commitments(thread.thread_key),
            thread_history="", reply_body=safe_body, reengage_hint=ex.reengage_hint)

        draft_text = llm.draft(prompt)

        # ---------- STAGE 10: VALIDATE ----------------------------------
        vctx = {"event": event, "allowed_facts": self.factbook.as_block(answered),
                "answered_questions": [a["q"] for a in answered],
                "sender_first": signer,
                "max_words": 120, "sendable_assets": self.factbook.sendable_assets}
        vres = validator.validate(draft_text, vctx)

        # exactly ONE regeneration attempt, then we stop trying
        if not vres["pass"]:
            S.log(msg.message_id, "VALIDATE_FAIL_1", vres["violations"])
            prompt["user"] += ("\n\nYour previous attempt was rejected for: "
                               + "; ".join(vres["violations"]) + "\nRewrite it.")
            draft_text = llm.draft(prompt)
            vres = validator.validate(draft_text, vctx)

        # ---------- STAGE 10b: CRITIC (LLM CALL 3) ----------------------
        if vres["pass"]:
            cal_block = event.human_time if event else "NO EVENT EXISTS"
            crit = llm.critique(safe_body, draft_text,
                                self.factbook.as_block(answered), cal_block,
                                ex.open_questions)
            if not crit["pass"]:
                vres = {"pass": False, "violations": ["critic: " + x for x in crit["violations"]]}

        # ---------- STAGE 10c: rehydrate, then check nothing leaked -------
        if red.leaked(draft_text):
            vres = {"pass": False,
                    "violations": ["a redaction placeholder survived into the draft"]}
        draft_text = red.rehydrate(draft_text)

        o.draft = draft_text
        o.validation = vres

        if not vres["pass"]:
            # Validation failure ALWAYS goes to a human, even on the allowlist.
            if event:
                self._compensate(thread, event, "draft failed validation")
                o.event = None
            S.enqueue_review(msg.message_id, thread.thread_key,
                             {"from": ident.real_sender, "body": msg.body[:900],
                              "intent": ex.action_intent, "draft": draft_text,
                              "reasons": vres["violations"]},
                             "Gate 3", "validation")
            o.action = "QUEUED_VALIDATION_FAILED"
            S.mark_processed(msg.message_id, o.action)
            return o

        # ---------- STAGE 11: SEND, or queue for approval ---------------
        if dec.route == "AUTO_SEND":
            self._enqueue_outbound(msg, ident, contact, thread, draft_text, tz)
            thread.machine_turns += 1
            o.action = "AUTO_SEND_QUEUED"
        else:
            S.enqueue_review(msg.message_id, thread.thread_key,
                             {"from": ident.real_sender, "org": contact.org,
                              "body": msg.body[:900], "intent": ex.action_intent,
                              "draft": draft_text, "reasons": dec.reasons,
                              "event": event.human_time if event else None},
                             dec.gate or "Gate 1", dec.binding_constraint)
            o.action = "QUEUED_WITH_DRAFT"

        # ---------- state transitions & bookkeeping ---------------------
        thread.reply_count += 1
        if event:
            thread.state = "CALL_BOOKED"
            thread.calendar_event_id = event.event_id
        elif ex.action_intent in ("WANTS_INFO", "ANSWERABLE_QUESTION"):
            thread.state = "CALL_OFFERED"
        elif ex.action_intent == "DECLINE":
            thread.state = "CLOSED"
        else:
            thread.state = "ENGAGED"
        self.store.upsert_thread(thread)

        if ex.action_intent == "DEFER":
            days = self.policy["defer_defaults_days"].get(ex.defer_subtype, 90)
            S.suppress(contact.email, days=days,
                       reason=f"DEFER/{ex.defer_subtype}: {ex.reengage_hint}")
            S.add_commitment(Commitment(thread.thread_key,
                                        f"re-engage after {days} days ({ex.reengage_hint})",
                                        "FOLLOW_UP", "MACHINE",
                                        due_at=(datetime.now(timezone.utc)
                                                + timedelta(days=days)).isoformat()))
        if ex.action_intent == "DECLINE":
            S.suppress(contact.email, days=365, reason="declined")

        S.mark_processed(msg.message_id, o.action)
        return o

    # =================================================================
    def _schedule(self, msg, ident, contact, thread, ex, tz, o):
        S = self.store
        cfg = self.policy["scheduling"]

        # idempotency check #1: our own database
        existing = S.get_event(thread.thread_key)
        if existing:
            S.log(msg.message_id, "SCHEDULE", "event already exists for this thread")
            return None

        # idempotency check #2: ask Google directly
        if self.calendar.find_by_thread(thread.thread_key):
            S.log(msg.message_id, "SCHEDULE", "calendar already holds an event for this thread")
            return None

        try:
            now = datetime.now(ZoneInfo(cfg["organiser_timezone"]))
            horizon = now + timedelta(days=cfg["max_lead_business_days"] * 2)
            busy = self.calendar.free_busy(now, horizon, cfg["organiser_calendar"])
            slot = scheduler.pick_slot(now, busy, cfg, tz, ex.stated_availability)
            if slot is None:
                S.log(msg.message_id, "SCHEDULE", "no slot available in the window")
                return None
            video = (ex.channel_preference or contact.channel_preference) != "phone"
            ev = self.calendar.create_event(
                start=slot, tz_name=tz, attendee_email=ident.real_sender,
                attendee_name=contact.org or contact.name or ident.sender_name,
                organiser=ident.outbound_mailbox or "team@cicero.com",
                thread_key=thread.thread_key,
                duration=cfg["duration_minutes"], video=video)
            S.record_event(thread.thread_key, ev.event_id, ev.start_iso)
            S.log(msg.message_id, "SCHEDULE", f"created {ev.event_id} at {ev.human_time}")
            return ev
        except Exception as e:
            S.log(msg.message_id, "SCHEDULE_ERROR", str(e))
            return None

    def _compensate(self, thread, event, reason):
        """If we booked but cannot send, undo the booking. Otherwise they get
        a calendar invite with no explanation, which is worse than nothing."""
        try:
            self.calendar.cancel(event.event_id)
            self.store.cancel_event_record(thread.thread_key)
            self.store.log("-", "COMPENSATE", f"cancelled {event.event_id}: {reason}")
        except Exception as e:
            self.store.log("-", "COMPENSATE_FAILED", str(e))

    def _enqueue_outbound(self, msg, ident, contact, thread, body, tz):
        """Never send instantly. A jittered delay inside business hours
        buys a cancellation window and stops the reply reading as a bot."""
        s = self.policy["send"]
        delay = random.randint(s["delay_minutes_min"], s["delay_minutes_max"])
        send_at = datetime.now(timezone.utc) + timedelta(minutes=delay)
        if s["business_hours_only"]:
            local = send_at.astimezone(ZoneInfo(tz))
            bh = self.policy["scheduling"]["business_hours"]
            if local.hour < bh["start"]:
                local = local.replace(hour=bh["start"], minute=random.randint(0, 30))
            elif local.hour >= bh["end"] or local.weekday() >= 5:
                days_ahead = 1 + (2 if local.weekday() == 4 else 0)
                local = (local + timedelta(days=days_ahead)).replace(
                    hour=bh["start"], minute=random.randint(0, 45))
            send_at = local.astimezone(timezone.utc)

        self.store.enqueue_send(
            thread_key=thread.thread_key, to_email=ident.real_sender,
            from_mailbox=ident.outbound_mailbox or "team@cicero.com",
            subject=msg.subject, body=body,
            in_reply_to=msg.message_id,
            references_hdr=" ".join(ident.references),
            send_after=send_at.isoformat())

    # =================================================================
    def _handle_non_reply(self, msg, ident, verdict, reason, extras):
        S = self.store
        S.log(msg.message_id, "PREFILTER", f"{verdict}: {reason}")
        email_addr = ident.real_sender

        if verdict == "OPT_OUT":
            S.suppress(email_addr, days=None, reason="opt-out")
            return "SUPPRESSED_NO_REPLY"
        if verdict == "LEGAL_FREEZE":
            S.suppress(email_addr, days=None, reason="legal")
            S.enqueue_review(msg.message_id, ident.thread_key,
                             {"from": email_addr, "body": msg.body[:900],
                              "reasons": [reason]}, "Gate 2", "legal")
            return "FROZEN_ESCALATED"
        if verdict == "BOUNCE":
            c = S.get_contact(email_addr)
            if c:
                c.status = "UNDELIVERABLE"
                S.upsert_contact(c)
            return "MARKED_UNDELIVERABLE"
        if verdict == "CONTACT_DEPARTED":
            c = S.get_contact(email_addr)
            if c:
                c.status = "STALE"
                S.upsert_contact(c)
            S.enqueue_review(msg.message_id, ident.thread_key,
                             {"from": email_addr, "body": msg.body[:600],
                              "reasons": ["contact has left -- re-research the org"]},
                             "Gate 1", "stale_contact")
            return "CONTACT_STALE"
        if verdict == "OOO":
            S.suppress(email_addr, days=10, reason=f"OOO {extras.get('return_hint','')}")
            return "OOO_SUPPRESSED"
        if verdict == "INBOUND_INVITE":
            S.enqueue_review(msg.message_id, ident.thread_key,
                             {"from": email_addr, "reasons": [reason]}, "Gate 2", "inbound_invite")
            return "INVITE_ESCALATED"
        if verdict == "HUMAN_ONLY":
            S.enqueue_review(msg.message_id, ident.thread_key,
                             {"from": email_addr, "body": msg.body[:900],
                              "reasons": [reason]}, "Gate 2", "prefilter")
            return "QUEUED_NO_DRAFT"
        return "NO_ACTION"

    # =================================================================
    def flush_outbox(self):
        """Called by a separate worker. Sends anything whose delay has expired
        and which nobody cancelled in the meantime."""
        sent = 0
        for row in self.store.due_sends():
            if self.dry_run:
                self.store.mark_sent(row["id"], "DRY_RUN")
                continue
            try:
                self.gmail.send_reply(
                    from_mailbox=row["from_mailbox"], to_email=row["to_email"],
                    subject=row["subject"], body=row["body"],
                    in_reply_to=row["in_reply_to"], references=row["references_hdr"])
                self.store.mark_sent(row["id"], "SENT")
                sent += 1
            except Exception as e:
                self.store.mark_sent(row["id"], "FAILED", str(e))
                self.store.log("-", "SEND_FAILED", str(e))
        return sent
