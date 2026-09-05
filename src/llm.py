"""
llm.py -- the ONLY file that talks to a language model.

Three calls, and none of them makes a decision:
  1. extract()  -- reads the email, returns structured facts
  2. draft()    -- writes prose from facts it is given
  3. critique() -- a second, independent pass that tries to fail the draft

Everything else in the system is deterministic Python. That separation is
the whole design: if the model is wrong, it is wrong about a *field*, and
a rule catches it. It is never wrong about whether to send.

MOCK MODE: if no ANTHROPIC_API_KEY is set, a keyword-based stand-in runs
instead. The prototype is fully runnable with zero credentials, which is
what you want for a demo. Set the key to switch to the real model.
"""
import os, json, re, textwrap
from typing import Dict, Any, List
from .models import Extraction

MODEL = os.environ.get("CICERO_MODEL", "claude-sonnet-4-6")
USE_MOCK = not os.environ.get("ANTHROPIC_API_KEY")

INTENTS = ["BOOK_NOW", "WANTS_INFO", "ANSWERABLE_QUESTION", "DEFER", "DECLINE",
           "WRONG_PERSON", "NEEDS_HUMAN", "NON_REPLY"]


# ------------------------------------------------------------------ client
def _call(system: str, user: str, max_tokens=1200, temperature=0.0) -> str:
    if USE_MOCK:
        raise RuntimeError("mock mode -- no API call should reach here")
    import anthropic
    client = anthropic.Anthropic()
    r = client.messages.create(
        model=MODEL, max_tokens=max_tokens, temperature=temperature,
        system=system, messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if b.type == "text")


def _json_or_none(txt: str):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None


# ================================================================ CALL 1
EXTRACT_SYSTEM = """You read replies to cold outreach sent by Cicero, a firm that
acquires small and mid-sized Indian businesses. You extract structured facts.

You do NOT decide what to do. You do not write replies. You only report what
the message says.

Return ONE JSON object and nothing else. Schema:

{
  "action_intent": one of BOOK_NOW | WANTS_INFO | ANSWERABLE_QUESTION | DEFER |
                   DECLINE | WRONG_PERSON | NEEDS_HUMAN | NON_REPLY,
  "secondary_intents": [same vocabulary, things also present in the message],
  "sender_type_guess": FOUNDER | BROKER | UNKNOWN,
  "open_questions": [each distinct question they asked, verbatim-ish],
  "defer_subtype": HARD_DATED | EVENT_CONDITIONAL | SEASONAL | SOFT_BRUSHOFF |
                   CAPACITY | PROCESS_BLOCKED | UNKNOWN,
  "reengage_hint": "their own words about when to come back, or empty",
  "stated_availability": "any time or day they proposed, or empty",
  "timezone_hint": "IANA zone if inferable from signature, else empty",
  "channel_preference": "phone" | "video" | "",
  "referral_name": "", "referral_email": "",
  "deals": [ {"ref":"", "sector":"", "geography":"", "revenue_band":"",
              "doc_offered":"", "deadline":""} ],
  "sensitive_flags": [PRICING|LEGAL|VALUATION|COMPLAINT|PRESS|COMPETITOR|PERSONAL_DATA],
  "sentiment": POSITIVE | NEUTRAL | NEGATIVE,
  "confidence": 0.0-1.0,
  "reasoning": "one sentence"
}

Rules that matter:
- BOOK_NOW only when they have clearly agreed to or asked for a call.
  A short "sure" or "ok" is BOOK_NOW ONLY if the thread state given to you is
  CALL_OFFERED. Otherwise it is UNCLEAR, so use NEEDS_HUMAN.
- DEFER and DECLINE are different. "not right now" is DEFER. "not interested"
  is DECLINE. Getting this wrong is expensive.
- Any mention of price, fees, valuation, multiples, NDAs or lawyers sets a
  sensitive_flag, regardless of intent.
- Politeness is not interest. "This sounds great, let's revisit after our
  raise" is DEFER, sentiment POSITIVE.
- If the message is in Hindi, Hinglish or another Indian language, classify it
  normally but add "TRANSLATION" to sensitive_flags."""


def extract(body: str, subject: str, sender_type_known: str,
            thread_state: str, thread_history: str = "") -> Extraction:
    if USE_MOCK:
        return _mock_extract(body, thread_state)

    user = f"""<thread_state>{thread_state}</thread_state>
<sender_type_from_our_records>{sender_type_known}</sender_type_from_our_records>
<subject>{subject}</subject>
<prior_messages>{thread_history[:3000]}</prior_messages>
<reply>
{body[:6000]}
</reply>"""
    try:
        data = _json_or_none(_call(EXTRACT_SYSTEM, user)) or {}
    except Exception as e:
        return Extraction(action_intent="NEEDS_HUMAN", confidence=0.0,
                          reasoning=f"extraction failed: {e}")

    if data.get("action_intent") not in INTENTS:
        return Extraction(action_intent="NEEDS_HUMAN", confidence=0.0,
                          reasoning="model returned an unknown intent")

    ex = Extraction(
        action_intent=data.get("action_intent", "NEEDS_HUMAN"),
        secondary_intents=data.get("secondary_intents", []) or [],
        sender_type_guess=data.get("sender_type_guess", "UNKNOWN"),
        open_questions=data.get("open_questions", []) or [],
        defer_subtype=data.get("defer_subtype", "UNKNOWN"),
        reengage_hint=data.get("reengage_hint", "") or "",
        stated_availability=data.get("stated_availability", "") or "",
        timezone_hint=data.get("timezone_hint", "") or "",
        channel_preference=data.get("channel_preference", "") or "",
        referral_name=data.get("referral_name", "") or "",
        referral_email=data.get("referral_email", "") or "",
        deals=data.get("deals", []) or [],
        sensitive_flags=data.get("sensitive_flags", []) or [],
        sentiment=data.get("sentiment", "NEUTRAL"),
        confidence=float(data.get("confidence", 0.0) or 0.0),
        reasoning=data.get("reasoning", ""))
    return _sanity_check(ex, body, thread_state)


def _sanity_check(ex: Extraction, body: str, thread_state: str) -> Extraction:
    """Plausibility rules the model is not allowed to override."""
    words = len(body.split())
    # a two-word reply cannot justify booking unless we already offered
    if ex.action_intent == "BOOK_NOW" and words < 4 and thread_state != "CALL_OFFERED":
        ex.action_intent = "NEEDS_HUMAN"
        ex.confidence = min(ex.confidence, 0.3)
        ex.reasoning += " | overridden: too little evidence to book"
    # positive words plus a decline is a classic sarcasm trap
    if ex.sentiment == "POSITIVE" and ex.action_intent == "DECLINE":
        ex.confidence = min(ex.confidence, 0.6)
        ex.reasoning += " | sentiment/intent mismatch"
    # a DEFER that carries a question is not a DEFER
    if ex.action_intent == "DEFER" and ex.open_questions:
        ex.action_intent = "WANTS_INFO"
        ex.secondary_intents.append("DEFER")
        ex.reasoning += " | upgraded: deferral contained a question"
    return ex


# ================================================================ CALL 2
def build_draft_prompt(voice: Dict, sender_type: str, contact_name: str,
                       org: str, sender_first_name: str, intent: str,
                       questions_with_answers: List[Dict], event, ledger: List[str],
                       thread_history: str, reply_body: str, reengage_hint: str) -> Dict[str, str]:
    """Assembled here so it can be inspected and unit-tested without an API call."""
    facts_block = "\n".join(f"- Q: {q['q']}\n  APPROVED ANSWER: {q['a']}"
                            for q in questions_with_answers) or "- (none)"
    if event:
        event_block = (f"- A calendar invite HAS been created.\n"
                       f"- Exact time to state: {event.human_time}\n"
                       f"- Duration: 30 minutes\n"
                       f"- Link: {event.meet_link}")
    else:
        event_block = "- NO calendar invite exists. You must not mention any date or time."

    system = f"""{voice['identity']}

VOICE RULES (all mandatory):
{chr(10).join('- ' + r for r in voice['rules'])}

YOU MUST NEVER:
{chr(10).join('- ' + r for r in voice['never_say'])}

{voice['founder_notes'] if sender_type == 'FOUNDER' else voice['broker_notes']}

HARD CONSTRAINT: you may only assert facts that appear in the APPROVED FACTS or
CALENDAR blocks below. If you feel the need to state anything else -- a number,
a date, a promise, a detail about Cicero -- you must instead leave it out.
Write the email body only. No subject line. No preamble. No markdown."""

    user = f"""<recipient>
name: {contact_name} (first name: {contact_name.split()[0] if contact_name else 'there'})
type: {sender_type}
organisation: {org}
</recipient>

<you_are>{sender_first_name}</you_are>

<their_reply>
{reply_body[:3000]}
</their_reply>

<classified_intent>{intent}</classified_intent>
<their_own_words_on_timing>{reengage_hint or '(none)'}</their_own_words_on_timing>

<approved_facts_you_may_use>
{facts_block}
</approved_facts_you_may_use>

<calendar>
{event_block}
</calendar>

<outstanding_promises_we_have_made>
{chr(10).join('- ' + c for c in ledger) or '- (none)'}
</outstanding_promises_we_have_made>

<earlier_in_this_thread>
{thread_history[:2000]}
</earlier_in_this_thread>

Write the reply."""
    return {"system": system, "user": user}


def draft(prompt: Dict[str, str]) -> str:
    if USE_MOCK:
        return _mock_draft(prompt)
    return _call(prompt["system"], prompt["user"], max_tokens=600, temperature=0.4).strip()


# ================================================================ CALL 3
CRITIC_SYSTEM = """You are a compliance reviewer. You are shown an inbound email,
a proposed automated reply, and the complete list of facts the writer was allowed
to use. Your job is to FAIL the draft, not to be agreeable.

Fail it if ANY of these are true:
- it states a fact not present in the allowed facts or the calendar block
- it states a date, time or link not in the calendar block
- it mentions any number, price, fee, percentage or valuation
- it promises anything (a document, a follow-up, an introduction) not listed
- it claims a call is booked when the calendar block says none exists
- it ignores a question the sender explicitly asked
- it is over 120 words, or uses corporate filler, or sounds like a template
- the tone is wrong for the recipient type

Return ONLY: {"pass": true|false, "violations": ["..."]}"""


def critique(reply_body: str, draft_text: str, allowed_facts: str,
             calendar_block: str, questions: List[str]) -> Dict[str, Any]:
    if USE_MOCK:
        return _mock_critique(draft_text, questions)
    user = (f"<inbound>{reply_body[:2500]}</inbound>\n"
            f"<allowed_facts>{allowed_facts}</allowed_facts>\n"
            f"<calendar>{calendar_block}</calendar>\n"
            f"<questions_they_asked>{questions}</questions_they_asked>\n"
            f"<proposed_reply>{draft_text}</proposed_reply>")
    try:
        out = _json_or_none(_call(CRITIC_SYSTEM, user, max_tokens=500)) or {}
        return {"pass": bool(out.get("pass", False)),
                "violations": out.get("violations", ["critic returned no verdict"])}
    except Exception as e:
        # A critic that errors must FAIL CLOSED. Silence beats a bad send.
        return {"pass": False, "violations": [f"critic unavailable: {e}"]}


# ================================================================ MOCKS
# Deterministic stand-ins so the prototype runs with no API key at all.
_KW = {
    "BOOK_NOW":  [r"\bhappy to (?:talk|chat|jump on|hop on)", r"\bset up a call", r"\blet'?s (?:talk|chat|speak)",
                  r"\bcall works", r"\bworks for me", r"\bschedule (?:a )?call", r"\bsend (?:me )?an invite",
                  r"\btuesday|wednesday|thursday|monday|friday\b.*\b(work|good|fine)"],
    "DECLINE":   [r"\bnot interested", r"\bno thank", r"\bwe(?:'re| are) not selling", r"\bplease stop",
                  r"\bpass\b", r"\bnot for (?:us|me)", r"\bsold (?:the|our) (?:business|company)"],
    "DEFER":     [r"\bnot (?:right )?now", r"\brevisit", r"\bcircle back", r"\bnext (?:quarter|year)",
                  r"\bafter (?:our|the|march|april|diwali|tax)", r"\bping me in", r"\btoo early",
                  r"\bswamped", r"\balready in (?:a|an) (?:process|discussion)"],
    "WRONG_PERSON": [r"\bnot the right person", r"\bspeak (?:to|with) (?:my|our)", r"\bforward(?:ing)? (?:this|you)",
                     r"\bmy (?:colleague|partner|banker|advisor|cfo)"],
    "NEEDS_HUMAN": [r"\bnda\b", r"\bterm sheet", r"\bwhat (?:multiple|valuation)", r"\byour fee",
                    r"\bproof of funds", r"\blawyer|attorney"],
}
_Q = re.compile(r"[^.?!]*\?")


def _mock_extract(body: str, thread_state: str) -> Extraction:
    b = body.lower()
    hits = {k: sum(1 for p in pats if re.search(p, b)) for k, pats in _KW.items()}
    intent = max(hits, key=hits.get) if max(hits.values()) > 0 else None
    questions = [q.strip() for q in _Q.findall(body) if len(q.strip()) > 8][:4]

    if not intent:
        intent = "ANSWERABLE_QUESTION" if questions else "NEEDS_HUMAN"
    if hits["NEEDS_HUMAN"]:
        intent = "NEEDS_HUMAN"
    if len(body.split()) < 4 and thread_state == "CALL_OFFERED":
        intent, questions = "BOOK_NOW", []

    flags = []
    if re.search(r"\bnda|term sheet|lawyer|attorney|legal\b", b): flags.append("LEGAL")
    if re.search(r"\bfee|price|valuation|multiple|crore|lakh\b", b): flags.append("PRICING")

    defer_sub = "UNKNOWN"
    if intent == "DEFER":
        if re.search(r"\balready in (?:a|an) (?:process|discussion)", b): defer_sub = "PROCESS_BLOCKED"
        elif re.search(r"\bswamped|busy|bandwidth", b): defer_sub = "CAPACITY"
        elif re.search(r"\braise|series|funding|closes", b): defer_sub = "EVENT_CONDITIONAL"
        elif re.search(r"\btax season|diwali|holidays|year.?end", b): defer_sub = "SEASONAL"
        elif re.search(r"\b(march|april|june|q[1-4]|\d{1,2}(st|th|nd|rd))", b): defer_sub = "HARD_DATED"
        else: defer_sub = "SOFT_BRUSHOFF"

    avail = ""
    m = re.search(r"\b(mon|tues|wednes|thurs|fri)day[^.,;]{0,40}", b)
    if m: avail = m.group(0)

    ex = Extraction(action_intent=intent, open_questions=questions,
                    defer_subtype=defer_sub, stated_availability=avail,
                    sensitive_flags=flags,
                    sentiment="NEGATIVE" if intent == "DECLINE" else "NEUTRAL",
                    confidence=0.88 if max(hits.values()) > 0 else 0.45,
                    reasoning="[MOCK] keyword match")
    if intent in ("BOOK_NOW", "WANTS_INFO") and questions:
        ex.secondary_intents = ["ANSWERABLE_QUESTION"]
    return _sanity_check(ex, body, thread_state)


def _mock_draft(prompt: Dict[str, str]) -> str:
    u = prompt["user"]
    name = re.search(r"first name: ([^)]+)\)", u)
    first = name.group(1).strip() if name else "there"
    signer = re.search(r"<you_are>(.*?)</you_are>", u)
    sign = signer.group(1).strip() if signer else "Team"
    intent = re.search(r"<classified_intent>(.*?)</classified_intent>", u)
    intent = intent.group(1) if intent else ""
    time_m = re.search(r"Exact time to state: (.+)", u)
    facts = re.findall(r"APPROVED ANSWER: (.+)", u)

    if intent == "BOOK_NOW" and time_m:
        body = (f"{first} — good. I've put thirty minutes in for {time_m.group(1).strip()}; "
                f"the invite is on its way. Move it if that clashes.")
    elif intent == "DECLINE":
        body = f"{first} — understood, thanks for the straight answer. I'll leave it there."
    elif intent == "DEFER":
        body = (f"{first} — that's fair. I'll come back when the timing is better. "
                f"If anything changes before then, just reply here.")
    elif facts:
        body = f"{first} — {facts[0].strip()} Worth a short call if you'd like to hear more."
    else:
        body = f"{first} — thanks for coming back to me. Would a short call be useful?"
    return f"{body}\n\n{sign}"


def _mock_critique(draft_text: str, questions: List[str]) -> Dict[str, Any]:
    v = []
    if len(draft_text.split()) > 120:
        v.append("over 120 words")
    if re.search(r"\b(excited|guarantee|definitely|exclusive)\b", draft_text, re.I):
        v.append("banned word")
    return {"pass": not v, "violations": v}
