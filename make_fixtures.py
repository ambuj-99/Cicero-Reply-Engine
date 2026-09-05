"""
make_fixtures.py -- writes the mock inbox.

These are not decoration. Each one exists to prove a specific branch works.
Write your own labelled set before you write any more code: the fixtures are
simultaneously your test suite, your eval set and your demo script.

The `expect` field on each is the answer key.
"""
import json, os
from datetime import datetime, timedelta, timezone

OUT = "data/fixtures"
os.makedirs(OUT, exist_ok=True)
NOW = datetime.now(timezone.utc)


def f(n, name, headers, body, expect, **kw):
    d = {"headers": headers, "body": body,
         "received_at": (NOW - timedelta(hours=kw.pop("hours_ago", 2))).isoformat(),
         "to": kw.pop("to", ["deals@cicero.com"]), "cc": kw.pop("cc", []),
         "expect": expect}
    d.update(kw)
    json.dump(d, open(f"{OUT}/{n:02d}_{name}.json", "w"), indent=2)


# 01 -- clean forward, founder wants a call. THE HAPPY PATH.
f(1, "founder_books", {
    "Message-ID": "<a1@kesarfoods.in>", "From": "deals@cicero.com",
    "X-Forwarded-For": "deals@cicero.com priya@kesarfoods.in",
    "In-Reply-To": "<out-priya-1@cicero-out3.com>",
    "Subject": "Re: Kesar Foods"},
  "Thanks for reaching out. Happy to jump on a call - Tuesday afternoon works "
  "best for me.\n\nPriya Nair\nKesar Foods, Mumbai",
  "AUTO_SEND or QUEUED_WITH_DRAFT, calendar event created")

# 02 -- polite deflection that LOOKS positive. Classic misfire.
f(2, "defer_disguised_as_yes", {
    "Message-ID": "<a2@vaultpackaging.com>", "From": "s.iyer@vaultpackaging.com",
    "Subject": "Re: Vault Packaging"},
  "This sounds genuinely interesting and I'd like to explore it. That said, "
  "we're mid-way through a funding round - let's revisit once that closes.\n\nSuresh",
  "DEFER / EVENT_CONDITIONAL, ~120 day suppression, NO calendar event")

# 03 -- one-word reply with NO prior offer. Must not book.
f(3, "bare_sure_no_context", {
    "Message-ID": "<a3@sunrisechem.in>", "From": "k.rao@sunrisechem.in",
    "Subject": "Re: Sunrise Chemicals"},
  "Sure.",
  "NEEDS_HUMAN -- 'sure' means nothing without a prior offer")

# 04 -- broker asking about our fee. HARD STOP.
f(4, "broker_fee_question", {
    "Message-ID": "<a4@apexadvisors.co.in>", "From": "rmehta@apexadvisors.co.in",
    "Subject": "Re: deal flow"},
  "Before I share anything, what's your fee arrangement with intermediaries? "
  "We typically work on 2% success fee. Also need proof of funds.\n\nRahul Mehta\nApex Advisors",
  "HUMAN_ONLY / Gate 2 -- fee, percentage and proof of funds all trip it")

# 05 -- out of office with a return date
f(5, "ooo_with_date", {
    "Message-ID": "<a5@northgatelabs.in>", "From": "founder@northgatelabs.in",
    "Auto-Submitted": "auto-replied", "Subject": "Automatic reply: Northgate"},
  "I am out of the office and will be back on 22 September. For urgent matters "
  "contact my office.",
  "OOO -- 10 day suppression, no reply sent, no LLM call")

# 06 -- hard opt-out
f(6, "opt_out", {
    "Message-ID": "<a6@somewhere.in>", "From": "owner@bharatspices.in",
    "Subject": "Re: Bharat Spices"},
  "Please remove me from your list and do not contact me again.",
  "OPT_OUT -- permanent suppression, absolutely no reply")

# 07 -- referral to a banker
f(7, "referral_to_banker", {
    "Message-ID": "<a7@vaultpackaging.com>", "From": "s.iyer@vaultpackaging.com",
    "Subject": "Re: Vault Packaging"},
  "I'm not the right person for this. Please speak to our advisor Deepa Menon "
  "at Menon Associates, dmenon@menonassociates.in.",
  "WRONG_PERSON -- human creates the new contact, no auto-reply")

# 08 -- founder asking two answerable questions
f(8, "answerable_questions", {
    "Message-ID": "<a8@kesarfoods.in>", "From": "priya@kesarfoods.in",
    "Subject": "Re: Kesar Foods"},
  "Who are you exactly, and how did you find me? I get a lot of these.",
  "ANSWERABLE_QUESTION -- both map to approved facts, coverage 1.0")

# 09 -- one answerable, one NOT answerable. Coverage must drop.
f(9, "partial_coverage", {
    "Message-ID": "<a9@sunrisechem.in>", "From": "k.rao@sunrisechem.in",
    "Subject": "Re: Sunrise Chemicals"},
  "What sort of businesses do you look at? And what happened with the last "
  "chemicals company you bought - did the team stay on?",
  "fact_coverage 0.5 -> DRAFT_FOR_REVIEW, binding constraint fact_coverage")

# 10 -- attachment. Never auto-handle.
f(10, "nda_attachment", {
    "Message-ID": "<a10@menonassociates.in>", "From": "dmenon@menonassociates.in",
    "Subject": "Re: mandate"},
  "Sign the attached NDA and I'll send the teaser.",
  "HUMAN_ONLY / Gate 2 -- attachment plus the word NDA",
  has_attachment=True)

# 11 -- inbound calendar invite. Must not counter-invite.
f(11, "inbound_invite", {
    "Message-ID": "<a11@apexadvisors.co.in>", "From": "rmehta@apexadvisors.co.in",
    "Subject": "Invitation: Apex x Cicero"},
  "Sending an invite for Thursday.",
  "INBOUND_INVITE -- accept or escalate, never create a second event",
  has_calendar_part=True)

# 12 -- three parties on the thread
f(12, "multi_party", {
    "Message-ID": "<a12@kesarfoods.in>", "From": "priya@kesarfoods.in",
    "Subject": "Re: Kesar Foods"},
  "Looping in my co-founder and our CA. Happy to talk.",
  "HUMAN_ONLY -- more than two parties raises the stakes",
  cc=["ca@kesarfoods.in", "cofounder@kesarfoods.in"])

# 13 -- forward where the sender is ONLY in the body. Low identity confidence.
f(13, "messy_forward", {
    "Message-ID": "<a13@cicero.com>", "From": "deals@cicero.com",
    "Subject": "Fwd: Re: your note"},
  "---------- Forwarded message ---------\n"
  "From: Anil Gupta <anil@guptatextiles.in>\n"
  "Date: Thu, 4 Sep 2026\n"
  "Subject: Re: your note\n\n"
  "Interested. Can we speak next week?",
  "identity_confidence 0.55 -> held, but the real sender IS recovered")

# 14 -- hostile decline
f(14, "hard_decline", {
    "Message-ID": "<a14@guptatextiles.in>", "From": "anil@guptatextiles.in",
    "Subject": "Re: your note"},
  "Not interested. We are not selling.",
  "DECLINE -- one-line close, 365 day suppression")

# 15 -- broker with a real deal, in thesis
f(15, "broker_deal", {
    "Message-ID": "<a15@menonassociates.in>", "From": "dmenon@menonassociates.in",
    "Subject": "Re: deal flow"},
  "I have a services business in Coimbatore that may suit. Owner-run, profitable, "
  "wants to retire. Can we set up a call this week to discuss?",
  "BOOK_NOW for a broker -- calendar event, broker-toned reply")

# 16 -- very long substantive reply
f(16, "long_substantive", {
    "Message-ID": "<a16@northgatelabs.in>", "From": "founder@northgatelabs.in",
    "Subject": "Re: Northgate Labs"},
  ("I've thought about this a lot. " * 60) + "So where does that leave us?",
  "HUMAN_ONLY -- long replies are always substantive")

print(f"wrote {len(os.listdir(OUT))} fixtures to {OUT}/")
