"""
router.py -- STAGE 7. Where the system decides what to do.

This file contains NO calls to a model. Every branch is a rule you can read,
test, and point at in a meeting. If someone asks "why did it send that?",
the answer is a line in this file plus a line in policy.yaml.

Four possible routes:
  AUTO_SEND         -- goes out after the delay, no human sees it first
  DRAFT_FOR_REVIEW  -- a draft is prepared and queued for approval
  HUMAN_ONLY        -- no draft is even attempted; a person handles it
  NO_ACTION         -- nothing is sent (declines, opt-outs, non-replies)
"""
import re, yaml
from typing import List
from .models import Decision, ConfidenceVector, Extraction, Identity, Thread


class Router:
    def __init__(self, policy_path="config/policy.yaml"):
        self.p = yaml.safe_load(open(policy_path))
        self.t = self.p["thresholds"]
        self.hs = self.p["hard_stops"]
        self.money = re.compile(self.hs["money_pattern"], re.I)
        self.allow = self.p["auto_send_allowlist"]

    # ------------------------------------------------------------------
    def decide(self, msg, identity: Identity, thread: Thread, ex: Extraction,
               fact_coverage: float, schedule_conf: float,
               state_conf: float = 1.0) -> Decision:

        cv = ConfidenceVector(identity=identity.confidence, intent=ex.confidence,
                              fact_coverage=fact_coverage, schedule=schedule_conf,
                              state=state_conf)
        reasons: List[str] = []
        body = msg.body or ""

        # ============ TIER 1: hard stops. Nothing overrides these. ============
        if ex.sensitive_flags:
            return self._stop(cv, f"sensitive flags {ex.sensitive_flags}", "Gate 2")

        if self.money.search(body):
            return self._stop(cv, "a monetary figure appears in their message", "Gate 2")

        for kw in self.hs["sensitive_keywords"]:
            if re.search(rf"\b{re.escape(kw)}\b", body, re.I):
                return self._stop(cv, f"sensitive keyword: '{kw}'", "Gate 2")

        if self.hs["block_on_attachment"] and msg.has_attachment:
            return self._stop(cv, "message carries an attachment", "Gate 2")

        recips = len(set(msg.to_recipients + msg.cc_recipients))
        if recips > self.hs["max_recipients"]:
            return self._stop(cv, f"{recips} parties on the thread", "Gate 2")

        if len(body.split()) > self.hs["max_body_words"]:
            return self._stop(cv, "unusually long reply -- likely substantive", "Gate 2")

        if thread.machine_turns >= self.hs["max_machine_turns"]:
            return self._stop(cv, f"{thread.machine_turns} consecutive automated sends already",
                              "Gate 1")

        # ============ TIER 2: terminal intents. No reply needed. ============
        if ex.action_intent == "NON_REPLY":
            return Decision(route="NO_ACTION", reasons=["not a genuine reply"], confidence=cv)

        if ex.action_intent == "NEEDS_HUMAN":
            return self._stop(cv, "classifier could not resolve a safe intent", "Gate 1")

        if ex.action_intent == "WRONG_PERSON":
            # Referrals create a NEW contact. That is a human decision.
            return self._stop(cv, "referral to another person", "Gate 1")

        # ============ TIER 3: confidence floor ============
        name, val = cv.minimum()
        floors = {"identity": self.t["identity_confidence_min"],
                  "intent": self.t["intent_confidence_min"],
                  "fact_coverage": self.t["fact_coverage_min"],
                  "schedule": self.t["schedule_confidence_min"],
                  "state": 0.8}
        for sig, floor in floors.items():
            got = getattr(cv, sig)
            if got < floor:
                return Decision(route="DRAFT_FOR_REVIEW",
                                reasons=[f"{sig} {got} is below the floor of {floor}"],
                                binding_constraint=sig, gate="Gate 1", confidence=cv)

        # ============ TIER 4: the auto-send allowlist ============
        for rule in self.allow:
            if rule["intent"] == ex.action_intent and \
               (rule["state"] == "*" or rule["state"] == thread.state):
                return Decision(route="AUTO_SEND",
                                reasons=[f"allowlisted: state={rule['state']} intent={rule['intent']}"],
                                binding_constraint=name, confidence=cv)

        # ============ TIER 5: default. Draft, but a human presses send. ======
        return Decision(route="DRAFT_FOR_REVIEW",
                        reasons=[f"intent {ex.action_intent} is not on the auto-send allowlist"],
                        binding_constraint=name, gate="Gate 1", confidence=cv)

    # ------------------------------------------------------------------
    def _stop(self, cv, reason, gate):
        return Decision(route="HUMAN_ONLY", reasons=[reason], gate=gate,
                        binding_constraint=cv.minimum()[0], confidence=cv)

    # ------------------------------------------------------------------
    def should_schedule(self, ex: Extraction, thread: Thread) -> bool:
        """
        STAGE 8 trigger. Deliberately narrow.

        A calendar invite is created only when the person has actually agreed
        to a call. Two ways that happens:
          1. they explicitly ask for one (BOOK_NOW), or
          2. we offered one last turn and they said yes.
        Nothing else books. Enthusiasm is not consent to a meeting.
        """
        if ex.sensitive_flags:
            return False
        if ex.action_intent == "BOOK_NOW":
            return True
        if thread.state == "CALL_OFFERED" and ex.sentiment != "NEGATIVE" \
           and ex.action_intent in ("BOOK_NOW", "WANTS_INFO"):
            return True
        return False
