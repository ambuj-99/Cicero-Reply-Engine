# Design Rationale — Cicero Automated Reply Engine

This document explains *why* the system is built the way it is. It answers the
six required questions in order, with worked examples from real fixtures and
the verbatim text of every prompt sent to a model.

The companion `README.md` covers setup and how to run it. The code is the
authority; where this document and the code disagree, the code is right.

---

## 0. The one idea the whole design rests on

**The language model reads and writes. It never decides.**

Every consequential choice — whether to reply, what route to take, which time
to book, whether to send — is made by deterministic Python reading a YAML
config. The model's job is to turn unstructured text into structured fields
(call 1), turn structured fields back into prose (call 2), and attack its own
output (call 3).

This is not caution for its own sake. It has three concrete consequences that
make the other five answers possible:

1. **Every decision is auditable.** "Why did it send that?" resolves to a line
   number and a config value, not a probability.
2. **Failures are bounded.** If the model is wrong, it is wrong about a *field*.
   A rule catches the field. It is never wrong about whether to press send.
3. **The system is testable.** The riskiest functions — slot selection and
   routing — are pure. 33 unit tests cover them with no network and no clock.

The corollary is that most of the engineering effort went into the parts that
are *not* the model. That allocation is itself the answer to "where did you
spend your effort."

---

## 1. Intent Classification

### 1.1 Founder vs broker is not a classification problem

The brief asks how the system distinguishes a founder from a broker. The honest
answer is that it mostly doesn't need to, because **we already know**.

Cicero ran the outreach. Every address in the primary inbox came from a
campaign list, and that list knows which bucket it was. So sender type is a
database lookup, not an inference:

```python
# pipeline.py
sender_type = contact.sender_type if contact.sender_type != "UNKNOWN" \
    else ex.sender_type_guess
```

The model *does* return a `sender_type_guess`, but it is used only when our own
records have nothing — an unknown sender, a forwarded thread, a reply from a
colleague we never contacted. It **never overrides** the contact record.

This matters more than it looks. A classifier reading the email body will call
"I have three listings that might suit" a broker, correctly, and will also call
a founder who happens to write formally a broker, incorrectly. The campaign list
has none of that variance. Using a model where a lookup will do is the most
common way to make a system worse.

**Where this breaks and what catches it:** a broker replying on a founder's
behalf. Sender type resolves correctly (they're a broker), but the *deal context*
is the founder's, and nothing in the current build models that. This is named as
an unbuilt gap in section 5.

### 1.2 Intents are defined by the action they cause

The obvious taxonomy — interested / not interested / question / schedule — fails
because two of those labels produce the same next step and one of them means
different things depending on context.

The rule I applied instead:

> **If two labels produce the same action, merge them. If one label means
> different things depending on thread state, it isn't a label — it's a state
> transition.**

That yields eight intents, each with exactly one downstream action:

| Intent | Action taken | Suppression written |
|---|---|---|
| `BOOK_NOW` | Create calendar event, then send confirmation | 14d pause |
| `WANTS_INFO` | Answer, offer a call, state → `CALL_OFFERED` | 14d pause |
| `ANSWERABLE_QUESTION` | Answer from approved facts only | 14d pause |
| `DEFER` | Extract re-engage date, suppress until then, short ack | per sub-type |
| `DECLINE` | One-line close | 365d |
| `WRONG_PERSON` | Human creates the new contact | halt this thread |
| `NEEDS_HUMAN` | Queue, no draft attempted | 14d pause |
| `NON_REPLY` | Nothing sent at all | varies |

Notice `ANSWERABLE_QUESTION` has a precondition inside the label. The model
returns the list of questions; **deterministic code** then checks whether each
maps to an approved fact. If any doesn't, the intent degrades to a hold. The
model is never asked "do you know the answer to this?" — a question it is
constitutionally unable to answer honestly.

### 1.3 `DEFER` is six intents wearing a trenchcoat

Most submissions will treat "not right now" as terminal. In Indian SMB M&A it is
the modal reply — my estimate is 30–40% of all responses — and treating it as a
dead end throws away most of the pipeline's value.

So `DEFER` carries a sub-type, and the sub-type sets the re-engagement date:

| Sub-type | Trigger phrasing | Days | Reply shape |
|---|---|---|---|
| `HARD_DATED` | "after March 31" | 7 (post-date) | Ack, confirm the date back |
| `EVENT_CONDITIONAL` | "once our raise closes" | 120 | Ack, one light qualifier |
| `SEASONAL` | "post tax season", "after Diwali" | 60 | Ack only |
| `CAPACITY` | "swamped, ping me in a few weeks" | 30 | Short, offer async |
| `PROCESS_BLOCKED` | "already in a process" | 90 | Establish standing for if it falls through |
| `SOFT_BRUSHOFF` | "not a priority" | 180 | One line, no ask |

`PROCESS_BLOCKED` is the commercially interesting one. Roughly a third of
processes collapse. A graceful "understood — if timing shifts, we're
straightforward to deal with" is the reason you get the call in month four.
It costs nothing to build and no other candidate will have it.

**One override worth calling out.** A `DEFER` that carries a question is not a
`DEFER`:

```python
# llm.py, _sanity_check
if ex.action_intent == "DEFER" and ex.open_questions:
    ex.action_intent = "WANTS_INFO"
    ex.secondary_intents.append("DEFER")
```

Answering their question and *then* deferring is what a person would do. Filing
it as a deferral and ignoring the question is what a bot does.

### 1.4 Classification runs against a delta, not a message

The single most important structural point: `"sure"` is meaningless in
isolation and unambiguous given context.

Three tiers of memory feed the classifier:

| Tier | Lifetime | Holds |
|---|---|---|
| **Contact** | Permanent | Identity, role, org, timezone, suppressions, history |
| **Thread** | Per conversation | State, machine-turn count, last human touch, commitments, event id |
| **Message** | Single | Raw signals, entities, identity resolution |

The thread state machine is `NEW → ENGAGED → CALL_OFFERED → CALL_BOOKED →
RESCHEDULING → CLOSED`, and it lives in SQLite, not in the model's head.

A hard rule enforces this:

```python
# llm.py -- a two-word reply cannot justify a booking on a cold thread
if ex.action_intent == "BOOK_NOW" and words < 4 and thread_state != "CALL_OFFERED":
    ex.action_intent = "NEEDS_HUMAN"
```

### 1.5 The actual prompt — LLM CALL 1

Verbatim from `src/llm.py`, `EXTRACT_SYSTEM`. Temperature 0.

```
You read replies to cold outreach sent by Cicero, a firm that acquires small
and mid-sized Indian businesses. You extract structured facts.

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
  normally but add "TRANSLATION" to sensitive_flags.
```

The user message:

```
<thread_state>{NEW | ENGAGED | CALL_OFFERED | CALL_BOOKED | ...}</thread_state>
<sender_type_from_our_records>{FOUNDER | BROKER | UNKNOWN}</sender_type_from_our_records>
<subject>{subject line}</subject>
<prior_messages>{trimmed thread history}</prior_messages>
<reply>
{the redacted reply body}
</reply>
```

Three design notes on this prompt:

- **Thread state is given first**, before the reply, because it changes how the
  reply should be read.
- **`sender_type_from_our_records` is supplied**, not asked for. The model can
  see what we already know, which stops it contradicting us on a coin flip.
- **The rules section names the expensive mistake explicitly** ("Getting this
  wrong is expensive") rather than describing the categories neutrally. Telling
  a model which error is costly measurably shifts its behaviour.

### 1.6 Worked example — fixture 02

The trap case. Positive language, zero intent.

**Input:**
> "This sounds genuinely interesting and I'd like to explore it. That said,
> we're mid-way through a funding round — let's revisit once that closes.
>
> Suresh"

**Extraction:**
```json
{"action_intent": "DEFER", "defer_subtype": "EVENT_CONDITIONAL",
 "sentiment": "POSITIVE", "reengage_hint": "once that closes",
 "open_questions": [], "confidence": 0.88}
```

**Route:** `AUTO_SEND` (DEFER is allowlisted)
**Actions:** 120-day suppression written, commitment logged, ack sent, **no
calendar event**.

The failure this avoids: a naive sentiment-driven classifier reads "genuinely
interesting" and "I'd like to explore it" and books a call with someone who
explicitly said not yet. That email is the embarrassing one.

### 1.7 Worked example — fixture 03

**Input:** `"Sure."`
**Thread state:** `NEW`

**Extraction:** `NEEDS_HUMAN`, confidence 0.45
**Route:** `HUMAN_ONLY`, Gate 1

Change nothing but the thread state to `CALL_OFFERED` and the same two
characters become `BOOK_NOW` with an event created. The message did not change.
The context did. That is the whole argument for tiered memory.

### 1.8 Where it misfires — named, not hedged

| Misfire | Realistic? | What catches it | Residual risk |
|---|---|---|---|
| Polite deflection read as interest | Very | `DEFER` is allowlisted but sends only an *ack*; booking requires `BOOK_NOW` | Low |
| Sarcasm — positive words, negative meaning | Occasional | Sentiment/intent mismatch caps confidence at 0.6 → held | Low |
| One-word replies | Very | Hard rule: `BOOK_NOW` needs ≥4 words unless `CALL_OFFERED` | Low |
| Multi-intent messages | Very | `secondary_intents` + validator checks each answered question is addressed | Medium |
| Hindi / Hinglish | Very, in this market | Flagged `TRANSLATION`, held | **High — unsolved** |
| Forwarded thread, last message isn't the reply | Common | Identity confidence 0.55 → held | Medium |
| Broker replying for a founder | Common | Sender type right, deal context wrong | **High — unsolved** |
| Signature bleeding into body | Very | `gmail_io.strip_quotes` | Low |
| Assistant/EA replying | Common | New sender on a known domain → unexpected respondent → held | Medium |

### 1.9 How I'd measure it

Not label accuracy — **route accuracy**. A classifier that says `WANTS_INFO`
instead of `ANSWERABLE_QUESTION` but routes to the same place has not made a
mistake that matters.

The errors are asymmetric, so the metric is too:

|  | Should auto-send | Should hold |
|---|---|---|
| **Did auto-send** | correct | **critical failure** |
| **Did hold** | cost: a few hours of latency | correct |

One headline number: **critical failure rate**. Target zero. Everything else is
supporting detail.

---

## 2. Reply Quality and Tone Control

### 2.1 The prompt is not the mechanism

Every candidate will write a prompt saying "don't hallucinate" and "sound
natural." Prompts are a weak instrument for this. The mechanism here is
structural.

**The model is only given the facts it is allowed to state.** Anything it might
want to say that isn't in the block simply isn't available, and if it needed
something that wasn't there, the message was already held before generation
began.

`config/approved_facts.yaml` holds a small curated set:

```yaml
facts:
  who_we_are:
    keywords: [who are you, what is cicero, what do you do, about you, your firm]
    text: >
      Cicero acquires and invests in established Indian small and mid-sized
      businesses. We are the buyer ourselves, not a broker.

  what_we_buy:
    keywords: [what kind, criteria, thesis, sectors, mandate, looking for]
    text: >
      We look at profitable, owner-run businesses with a track record.
      Sector-agnostic, with a bias toward services and light manufacturing.
```

Each question the sender asked is matched against this file deterministically
(`facts.py`). Coverage is `answered / total`. If coverage < 1.0, the router
holds — see fixture 09 below.

`sendable_assets` is deliberately an empty list. A validator rejects any draft
promising to send a document, because there are currently no documents approved
for automated sending. Overcommitment is prevented by the config being empty,
not by asking the model nicely.

### 2.2 Prompt architecture — seven blocks, fixed order

The order is not arbitrary. Constraints come before content, so the model reads
the rules before it reads anything it might want to break them for.

| # | Block | Purpose |
|---|---|---|
| 1 | Identity and brand voice | Who is writing |
| 2 | Hard prohibitions | What may never appear |
| 3 | Recipient-type notes | Founder framing vs broker framing |
| 4 | **Approved facts** | The only claims permitted |
| 5 | **Calendar block** | The only date permitted |
| 6 | Outstanding commitments | What we already promised |
| 7 | Their reply + thread history | What to respond to |

### 2.3 The actual prompt — LLM CALL 2

Verbatim from `llm.build_draft_prompt`. Temperature 0.4.

**System:**

```
You write as a member of the Cicero team. Cicero acquires and invests in
small and mid-sized Indian businesses. You are emailing either a founder
(an owner-operator) or a broker (an intermediary with deal flow).

VOICE RULES (all mandatory):
- Under 90 words. Shorter is better.
- Plain sentences. No corporate filler, no hope-this-finds-you-well openers.
- One ask per email, at most.
- Match their register. If they wrote two lines, do not write eight.
- Never use exclamation marks. Never use the word "excited".
- Sign with the sender's first name only.
- No bullet points in an email this short.

YOU MUST NEVER:
- Any number, price, fee, percentage, multiple or valuation
- Any promise about timing that is not in the approved facts
- The words guarantee, definitely, certainly will, exclusive, legally
- Anything about other deals, other founders, or other brokers
- That a call is booked, unless a real calendar event was supplied to you

{founder_notes OR broker_notes -- whichever applies}

HARD CONSTRAINT: you may only assert facts that appear in the APPROVED FACTS or
CALENDAR blocks below. If you feel the need to state anything else -- a number,
a date, a promise, a detail about Cicero -- you must instead leave it out.
Write the email body only. No subject line. No preamble. No markdown.
```

Where `founder_notes` is:

```
Founders care about who you are, why their business, and whether this wastes
their time. Be concrete and low-pressure. Never imply urgency you do not have.
```

and `broker_notes` is:

```
Brokers care about mandate fit, process and whether you are a real buyer.
Be businesslike and specific about thesis. Do not over-explain.
```

**User:**

```
<recipient>
name: Priya Nair (first name: Priya)
type: FOUNDER
organisation: Kesar Foods
</recipient>

<you_are>Arjun</you_are>

<their_reply>
{the redacted reply body}
</their_reply>

<classified_intent>BOOK_NOW</classified_intent>
<their_own_words_on_timing>(none)</their_own_words_on_timing>

<approved_facts_you_may_use>
- Q: Who are you exactly?
  APPROVED ANSWER: Cicero acquires and invests in established Indian small and
  mid-sized businesses. We are the buyer ourselves, not a broker.
</approved_facts_you_may_use>

<calendar>
- A calendar invite HAS been created.
- Exact time to state: Tuesday 08 Sep, 3:00pm-3:30pm IST
- Duration: 30 minutes
- Link: https://meet.google.com/abc-defg-hij
</calendar>

<outstanding_promises_we_have_made>
- (none)
</outstanding_promises_we_have_made>

<earlier_in_this_thread>
{trimmed}
</earlier_in_this_thread>

Write the reply.
```

When no event exists the calendar block reads:

```
- NO calendar invite exists. You must not mention any date or time.
```

### 2.4 The founder/broker split is substantive, not cosmetic

`sender_type` does not just change register. It changes what the email is
*about*:

**Founder** — Cicero's identity, why this business, low pressure, one soft ask.
The founder's question is "is this real and will it waste my time."

**Broker** — thesis fit, process, buyer credibility. The broker's question is
"is this a real buyer and does my mandate match." Over-explaining reads as
inexperience.

The system currently expresses this through the notes block and prompt framing.
**The right implementation is few-shot examples selected by
`(sender_type, intent)`** — 5–10 replies a human at Cicero actually wrote. That
is worth more than any amount of prompt tuning and it is the first thing I would
add. It is unbuilt because I don't have Cicero's real replies, and inventing
them would produce a voice that is confidently wrong.

### 2.5 Three independent layers stop a bad send

**Layer 1 — deterministic validators** (`validator.py`, 10 checks, zero cost):

| # | Check | Why |
|---|---|---|
| 1 | Length 8–120 words | Templates run long |
| 2 | Banned phrases | "guarantee", "excited", corporate filler |
| 3 | Money regex | No figure, ever |
| 4 | **Every date/time must appear in the booked event's `human_time`** | The critical one |
| 5 | Scheduling language requires an event id | Cannot claim a booking that doesn't exist |
| 6 | URL allowlist | Only our domains |
| 7 | No unresolved `{{placeholders}}` | Obvious tell |
| 8 | Every *answered* question is addressed | Not answering is a quality failure |
| 9 | Signature present | |
| 10 | No promise to send an unapproved document | Overcommitment |

Check 4 is the one that matters most:

```python
ht = ctx["event"].human_time.lower()
for token in set(x.lower() for x in dates):
    if token not in ht:
        v.append(f"time reference '{token}' is not in the booked slot")
```

The model cannot say "Thursday" when the calendar says Tuesday. The string has
to match.

**Layer 2 — exactly one regeneration.** Violations are fed back and the model
tries again. Once. Then it stops and goes to a human. No retry loop that
eventually produces something that passes by accident.

**Layer 3 — an independent critic** (LLM call 3), a separate prompt whose job is
to fail the draft:

```
You are a compliance reviewer. You are shown an inbound email, a proposed
automated reply, and the complete list of facts the writer was allowed to use.
Your job is to FAIL the draft, not to be agreeable.

Fail it if ANY of these are true:
- it states a fact not present in the allowed facts or the calendar block
- it states a date, time or link not in the calendar block
- it mentions any number, price, fee, percentage or valuation
- it promises anything (a document, a follow-up, an introduction) not listed
- it claims a call is booked when the calendar block says none exists
- it ignores a question the sender explicitly asked
- it is over 120 words, or uses corporate filler, or sounds like a template
- the tone is wrong for the recipient type

Return ONLY: {"pass": true|false, "violations": ["..."]}
```

The critic **fails closed**. If the API errors, the verdict is fail:

```python
except Exception as e:
    return {"pass": False, "violations": [f"critic unavailable: {e}"]}
```

An unavailable reviewer is not an approval.

### 2.6 Worked example — fixture 09, partial coverage

**Input:**
> "What sort of businesses do you look at? And what happened with the last
> chemicals company you bought — did the team stay on?"

**Extraction:** two `open_questions`.
**Coverage:** question 1 → `what_we_buy` ✓. Question 2 → no match ✗. **0.5**.
**Route:** `DRAFT_FOR_REVIEW`, binding constraint `fact_coverage`.

This is the design working. There is no approved fact about a previous
acquisition, so the model is never given the option to invent one. A human
answers that question, and if it recurs it becomes a new entry in
`approved_facts.yaml` — which is how the system's knowledge grows, deliberately
and under review.

---

## 3. Scheduling Trigger and Logic

### 3.1 Event before email — the ordering inversion

The failure almost every implementation of this will ship:

1. Model writes "great, I've put 30 minutes in for Tuesday at 2"
2. Calendar API call fails, or returns a different slot
3. The email goes out anyway

Inverted here:

```
1.  Deterministic slot picker runs against FreeBusy      -> a concrete slot
2.  Calendar event created                               -> real id, real time
3.  Those values injected into the drafting prompt AS FACTS
4.  Prompt: never state a time not in the facts block
5.  Validator: every datetime in the draft must appear in that block
6.  If the send later fails -> compensating cancel
```

```python
# pipeline.py
if event is None and ex.action_intent == "BOOK_NOW":
    # They asked for a call and we could not book. Do NOT write an email
    # that implies we did.
    S.enqueue_review(...)
    return o
```

And the compensation:

```python
def _compensate(self, thread, event, reason):
    self.calendar.cancel(event.event_id)
    self.store.cancel_event_record(thread.thread_key)
```

The model is a renderer, never a scheduler.

### 3.2 The trigger — deliberately narrow

`Router.should_schedule` returns `True` in exactly two situations:

```python
def should_schedule(self, ex, thread):
    if ex.sensitive_flags:
        return False
    if ex.action_intent == "BOOK_NOW":
        return True
    if thread.state == "CALL_OFFERED" and ex.sentiment != "NEGATIVE" \
       and ex.action_intent in ("BOOK_NOW", "WANTS_INFO"):
        return True
    return False
```

In words: they asked for a call, or we offered one last turn and they said yes.

**Enthusiasm is not consent to a meeting.** A `WANTS_INFO` reply with
`sentiment: POSITIVE` on a `NEW` thread does not book. It answers, offers, and
moves the state to `CALL_OFFERED`. The booking happens on the next turn, if they
take the offer.

This directly answers "inviting someone who didn't ask for a call": consent is
a state transition recorded in a database, not a vibe read off an email.

### 3.3 Slot selection is a pure function

```python
def pick_slot(now, busy, cfg, recipient_tz, stated_availability="") -> Optional[datetime]
```

No network. No `datetime.now()` inside. No randomness. Same inputs, same output.
Ten unit tests cover it.

Rules, applied in order:

| Rule | Value | Why |
|---|---|---|
| Minimum lead | 24h | Nobody wants an invite for a call in twenty minutes |
| Maximum horizon | 10 business days | A call three weeks out never happens |
| Business hours | **recipient's**, 10:00–18:00 | Booking your morning into their night is the classic bug |
| Weekends | excluded | |
| Holidays | from config | Diwali is not a Tuesday |
| Buffers | 15 min both sides | Back-to-back is double-booked in practice |
| Stated day | honoured exclusively | "Thursday works" means Thursday |
| Stated part of day | reordered search | "afternoon" searches 13:00 onward first |

Returns `None` rather than forcing a bad slot. `None` routes to a human.

### 3.4 Double-booking is prevented three times

Redundancy here is deliberate — each mechanism fails in a different way.

**1. Google FreeBusy.** Authoritative on the organiser's calendar.

**2. Thread-keyed idempotency in Google.** Before every create:

```python
r = self.svc.events().list(
    privateExtendedProperty=f"cicero_thread={thread_key}", ...)
```

The thread key is written into `extendedProperties.private` on creation. Even if
our database is lost, Google itself knows we already booked this thread.

**3. `PRIMARY KEY` on `thread_key`** in our own `events` table. The database
physically cannot hold two active events for one thread.

**One active event per thread, always.** A reschedule *moves* that event. It
never creates a second. This is the rule that prevents the worst scheduling
failure, which is not a clash — it's a founder with three competing invites from
the same firm.

**Concurrency:** two replies landing in the same thread simultaneously is not
hypothetical (a founder and their cofounder both reply). The send delay window
merges them in the prototype; at scale it needs a per-thread advisory lock, and
that's named in section 5.

### 3.5 Unknown timezone does not get a guess

```python
def resolve_timezone(signature_text, email, contact_tz, llm_hint, default_tz):
    if contact_tz:                     return contact_tz, 1.00
    if llm_hint (valid IANA):          return llm_hint, 0.85
    if signature matches "IST|Mumbai": return matched_tz, 0.85
    if email TLD is .in / .ae / .sg:   return tld_tz, 0.75
    return default_tz, 0.45            # below the floor -> the router holds
```

The confidence is the point. Below 0.80 the router holds rather than booking
someone a 3am call. The correct behaviour when you don't know where someone is
is not to guess — it's to send a booking link, or ask a human.

### 3.6 Direct-book, not propose-three-slots

A choice worth defending. Every extra round trip loses people, the brief asks
for an invite rather than a negotiation, and a founder who wanted a different
time can move a Google invite in two clicks.

So the system books one slot and always appends the escape hatch: *"move it if
that clashes."* `guestsCanModify: True` is set on the event so they actually can.

### 3.7 Worked example — fixture 01

**Input:**
> "Thanks for reaching out. Happy to jump on a call — Tuesday afternoon works
> best for me.
>
> Priya Nair, Kesar Foods, Mumbai"

**Trace:**
```
identity      priya@kesarfoods.in via X-Forwarded-For, conf 0.95
              owning mailbox arjun@cicero-out3.com
intent        BOOK_NOW, conf 0.88, 0 questions, no flags
timezone      Asia/Kolkata from the contact record, conf 1.0
should_schedule  True
freebusy      Tue 11:00-12:00 busy, Tue 15:00-16:30 busy
pick_slot     Tuesday 08 Sep 13:00 IST   <- Tuesday honoured, afternoon
                                            honoured, both busy blocks avoided
create_event  mock_8ae713ff7760, thread key written to extendedProperties
draft         renders 13:00, not any other time
validate      PASS -- the date in the draft matches human_time
route         DRAFT_FOR_REVIEW  (BOOK_NOW on a NEW thread is not allowlisted)
```

The last line is intentional and worth saying out loud in the interview. Even a
clean, high-confidence, correctly-booked first reply gets a human glance, because
`(NEW, BOOK_NOW)` is not on the allowlist yet. It gets promoted when the edit
rate proves it's safe — see 4.4.

---

## 4. Guardrails and Failure Handling

### 4.1 Confidence is a vector, not a scalar

A single confidence score hides *which part* of the system is unsure, which
makes the hold uninterpretable and the threshold unadjustable.

Five signals. **Only one comes from the model:**

| Signal | Source | Low means |
|---|---|---|
| `identity` | Deterministic header chain | We don't know who this is |
| `intent` | **Model, self-reported** | Ambiguous reply |
| `fact_coverage` | Deterministic | We'd have to invent an answer |
| `schedule` | Deterministic | Timezone unresolved |
| `state` | Deterministic | Thread history doesn't parse |

The router takes the **minimum** and logs the binding constraint:

```
confidence  id=0.55 intent=0.45 facts=0.0 sched=1.0 state=1.0 -> binding=identity
route       DRAFT_FOR_REVIEW  Gate 1 :: identity 0.55 is below the floor of 0.8
```

"Why did it hold?" always has a one-word answer.

Model-reported confidence is poorly calibrated and I don't pretend otherwise —
which is exactly why it's one of five signals rather than the whole decision,
and why sanity rules can override it outright.

### 4.2 The allowlist is inverted

Most designs auto-send unless confidence is low. This one auto-sends **only**
from a short explicit list in `config/policy.yaml`:

```yaml
auto_send_allowlist:
  - {state: CALL_OFFERED, intent: BOOK_NOW}
  - {state: "*",          intent: DECLINE}
  - {state: "*",          intent: DEFER}
```

Three cells. Everything else drafts and waits.

On the 16 fixtures: **2 auto-sent, 12 held, 2 suppressed with no reply.** That
looks timid until you look at *which* 12 — the broker asking about fees, the NDA
attachment, the three-party thread, the messy forward at 0.55 identity
confidence. Every one is a message where a confident wrong send costs a deal.

### 4.3 Four gates

**Gate 1 — off the allowlist.** Default. Not on the list → a human presses send.

**Gate 2 — hard stops.** Nothing overrides these, not even perfect confidence:

```python
if ex.sensitive_flags:                     return HUMAN_ONLY   # pricing, legal, press
if self.money.search(body):                return HUMAN_ONLY   # any figure they wrote
if any sensitive_keyword in body:          return HUMAN_ONLY   # NDA, LOI, lawyer, fee
if msg.has_attachment:                     return HUMAN_ONLY
if len(recipients) > 2:                    return HUMAN_ONLY   # lawyers get added silently
if len(body.split()) > 400:                return HUMAN_ONLY   # long = substantive
if thread.machine_turns >= 2:              return HUMAN_ONLY   # drift cap
```

The machine-turn cap bounds how far a conversation can travel without any human
seeing it. Two automated sends, then a person must touch the thread before a
third.

**Gate 3 — validation failure.** Overrides the allowlist. An `AUTO_SEND` route
whose draft fails validation becomes a hold, and any calendar event created for
it is cancelled.

**Gate 4 — the send delay.** Never send within seconds. A jittered 7–25 minute
delay inside the recipient's business hours. Three justifications, all real:

1. A reply arriving 8 seconds later reads as a bot and burns trust with exactly
   the sophisticated founders you want.
2. It creates a genuine cancellation window between processing and delivery.
3. It naturally merges the double-reply race.

Four lines of code. `run.py` and `run.py --flush` are separate cron jobs
precisely so this window is real.

### 4.4 The line moves on evidence

A fixed human boundary is a permanent tax. This one widens on measurement.

Every human action in the review queue is stored as a diff:

```python
store.resolve_review(row["id"], "EDITED",
                     json.dumps({"was": p.get("draft"), "now": new}))
```

`python review.py --stats`:

```
EDIT RATES BY GATE  (drives allowlist widening)
  Gate 1     resolved=52   edit/reject rate=8%  <- safe to automate
  Gate 2     resolved=31   edit/reject rate=61%
  Gate 3     resolved=9    edit/reject rate=44%
```

**The promotion rule:** when a `(state, intent)` cell's edit-and-reject rate
drops below 10% over 50 samples, it goes onto the allowlist. That is a config
change, not a code change, and it is reversible the same way.

This converts "where do you draw the line" from an opinion into a measurement,
and it means the honest answer to "is 2/16 too conservative?" is *"it is for
week one, and here is the mechanism that fixes it by week six."*

### 4.5 Why the line is *there*

The costs are asymmetric, so the system is asymmetric.

- Cost of holding a message: a few hours of latency. Recoverable.
- Cost of one confidently wrong send to a founder: the deal, and possibly the
  sending domain's reputation. Not recoverable.

Under that asymmetry, optimising for automation rate is the wrong objective
function. The right one is: **maximise automation subject to zero critical
failures**, and use the override rate to find out how far that lets you go.

### 4.6 Every failure degrades toward silence or a human

Never toward a template. The worst outcome is not a slow reply — it's a
confident wrong one.

| Failure | Behaviour |
|---|---|
| Model returns malformed JSON | One retry at temp 0, then hold |
| Model returns an unknown intent | Rejected outright → `NEEDS_HUMAN` |
| Model returns something implausible | Sanity rules override it |
| Draft fails validation | One regeneration, then hold |
| Critic API errors | **Fails closed** |
| LLM provider down | Queue and wait. No template fallback |
| Calendar create fails | Message queued; no email is written |
| Send fails after event creation | Compensating cancel |
| Duplicate delivery | `UNIQUE` on `message_id` |
| Redaction placeholder survives into a draft | Hard validation failure |
| Any unhandled exception | Caught in `run_once`, queued to a human, full traceback in `audit` |

```python
except Exception as e:
    self.store.log(msg.message_id, "CRASH", traceback.format_exc()[:1500])
    self.store.enqueue_review(msg.message_id, "?", {"error": str(e)}, "Gate 3", "exception")
```

A crash produces a queue item, not a dropped message.

### 4.7 The thing that runs before everything else

Stage 5, before classification, before any reasoning:

```python
S.suppress(contact.email, days=14, reason="replied -- pausing sequence")
```

The most embarrassing production failure in an outreach system is not tone. It
is follow-up #3 landing after someone already booked a call. This write happens
first so it survives a crash in every later stage.

---

## 5. Architecture and Reasoning

### 5.1 The eleven stages

```
 0  dedupe on Message-ID                                  store.py
 1  fetch from the primary inbox                          gmail_io.py    [Gmail API]
 2  resolve identity: real sender + owning mailbox        identity.py
 3  filter non-replies: OOO, bounces, opt-outs, invites   prefilter.py
 4  load contact, thread state, commitment ledger         store.py
 5  SUPPRESS THE SEQUENCER                                store.py
 5b REDACT before anything leaves the machine             redact.py
 6  extract intent, entities, questions                   llm.py         [LLM call 1]
 6b fact coverage check                                   facts.py
 7  route: confidence vector -> allowlist                 router.py
 8  pick a slot, CREATE THE EVENT                         scheduler.py   [Calendar API]
 9  draft the reply from real facts                       llm.py         [LLM call 2]
10  validate: rules, then a critic, then rehydrate        validator.py   [LLM call 3]
11  send after a jittered delay, in-thread                gmail_io.py    [Gmail API]
```

Four ordering choices carry the argument, and each is defensible in one line:

- **5 before 6** — suppress before you think, so it survives a crash.
- **5b before 6** — nothing sensitive leaves the machine, ever.
- **6b before 9** — check what you're allowed to say before generating.
- **8 before 9** — book before you promise.

### 5.2 The problem nobody else will notice

The brief contains one sentence with the hardest engineering problem in it:

> *"Gmail forwarding rules route every response into a single primary inbox."*

Forwarding rewrites the envelope. `From:` becomes the forwarding mailbox, not
the founder. Threading headers may be mangled. And the reply must go out **from
the original outbound mailbox** — reply from `deals@cicero.com` to a thread the
founder had with `arjun@cicero-out3.com` and you break thread continuity in
their client, look automated, and damage a domain that never warmed to them.

`identity.py` resolves through four methods in reliability order:

| Method | Confidence | When |
|---|---|---|
| `X-Forwarded-For` | 0.95 | Gmail filter forward — most common |
| `Reply-To` disagreeing with `From` | 0.90 | |
| `From` is external | 0.95 | The simple case |
| Regex on the forwarded body block | **0.55** | Last resort → held |

Separately it resolves the owning mailbox: thread lineage via `References`
(certain) → contact record → any of our addresses in To/Cc (0.70).

Fixture 13 proves it. `From: deals@cicero.com`, real sender buried in the body.
The system recovers `anil@guptatextiles.in`, drops confidence to 0.55 because it
had to parse prose, and holds. Correct on both counts.

### 5.3 Tool choices

| Choice | Why | At scale |
|---|---|---|
| Plain Python scripts | Readable top to bottom, no framework to learn | Same code in a queue worker |
| SQLite | One file, zero setup, but **real transactions and UNIQUE constraints** — which is what actually prevents double-sends | Postgres |
| Cron, two jobs | Two lines, obvious what runs when, and the split makes the delay window real | Pub/Sub push |
| Gmail API | Only way to send in-thread from a specific alias with correct headers | Same |
| Google Calendar API | FreeBusy is the only reliable double-booking guard | Same |
| YAML config | Business rules change weekly; code shouldn't | Same, version-controlled |
| CLI review queue | The interesting problem is *where* the human sits, not the UI | Web app or Slack |

The SQLite justification is the non-obvious one. A JSON file would be simpler
and would also make the dedupe and one-event-per-thread guarantees impossible.
Those two constraints are doing real safety work.

### 5.4 Deliberately left out

Naming your own cuts is more useful than hoping nobody notices them.

| Cut | Why | Cost |
|---|---|---|
| CRM integration | Flat CSV proves the model; a HubSpot client proves nothing | Low |
| Push notifications | Cron polling is 2 lines vs an hour of Pub/Sub setup | Low |
| Multi-rep round-robin | Single calendar; routing is orthogonal | Low |
| Attachment parsing | Attachments are a hard stop anyway | Low |
| Non-English handling | Flagged and held | **High in this market** |
| Broker deal extraction | Schema exists in `Extraction.deals`; nothing consumes it | **High if the mix is broker-heavy** |
| Web review UI | Least interesting part of the problem | Low |
| Re-engagement worker | `DEFER` dates are stored; nothing fires on them yet | Medium |
| Few-shot examples from real replies | I don't have Cicero's real voice | **High for quality** |

The last three are what I'd build next, in that order.

### 5.5 At hundreds a day

Five changes, in order of importance:

1. **A work queue keyed by `thread_key`.** Two replies in one thread must not
   process concurrently. This is the main *correctness* risk at volume, not
   throughput.
2. Gmail `watch()` → Pub/Sub push, not polling. Latency and quota.
3. Postgres, with a per-thread advisory lock.
4. A small fast model for extraction and the critic, a larger one for drafting.
   Extraction is a structured-output task; it doesn't need the frontier model.
5. **Human override rate becomes the control loop.** If it drops, widen the
   allowlist. If it spikes, narrow it. That number is the system's health
   metric — not messages processed.

What does *not* change: the eleven stages, the ordering, the confidence vector,
and the fact that the model never decides anything. The prototype's architecture
is the production architecture with different infrastructure underneath, which
is the point of putting all the business rules in YAML.

---

## 6. Data Handling

### 6.1 The trade-off, stated rather than hidden

Most answers to this question say "we redact PII" and stop. The interesting
part is what you *don't* redact and why.

**We do not redact names or company names.** Pseudonymising them would destroy
the tone quality that is the entire point of this system. "Dear PERSON_1,
regarding COMPANY_2" cannot be made to sound like a thoughtful colleague, and
the model needs the real name and org to pick the right register.

**We do redact everything that is both sensitive and useless for tone.** None of
the following improves a reply. All of them are painful in a breach.

This is a conscious trade, not an oversight, and I'd rather be asked about it
than have it discovered.

### 6.2 What `redact.py` catches

Reversible, typed placeholders. Ordered longest-and-most-specific first, so a
GSTIN isn't partially eaten by the generic number rule.

| Class | Patterns |
|---|---|
| Indian identity/tax | GSTIN, PAN, Aadhaar, CIN |
| Financial accounts | IFSC, account numbers, card numbers |
| Contact | Phone (6 formats: `+91 98765 43210`, `9876543210`, `098765-43210`, international), URLs |
| **Money** | `₹`/`Rs`/`INR`/`USD` amounts, and bare `4.2 crore` / `50 lakh` |
| Location | Postal address lines, PIN codes |

Exact monetary figures are the single most sensitive item in an M&A thread and
the model has zero legitimate use for them — it's forbidden from repeating a
number anyway. Redacting them costs nothing and removes the highest-value target
from the wire.

**Verified behaviour:**

```
Input:  "We did about 4.2 crore revenue last year. Call me on +91 98765 43210.
         GSTIN 27AAPFU0939F1ZV, PAN ABCDE1234F.
         Office: 12/A, Linking Road, Bandra West - 400050.
         Priya Nair, Kesar Foods."

Sent:   "We did about [MONEY_1] revenue last year. Call me on [PHONE_1].
         GSTIN [GSTIN_1], PAN [PAN_1]. Office: [ADDRESS_1].
         Priya Nair, Kesar Foods."

Logged: {'MONEY': 1, 'PHONE': 1, 'GSTIN': 1, 'PAN': 1, 'ADDRESS': 1}
```

Names survive. Everything else is a token. Also verified: `"12 people on the
team"` is left alone — the money pattern requires a currency marker or an
Indian magnitude word, so it doesn't eat ordinary counts.

### 6.3 Scope and lifetime

- **One `Redactor` instance per message.** The mapping lives in memory for the
  duration of one request and then goes out of scope. It is never written to
  disk, never logged, never persisted.
- **Rehydration is local.** Placeholders come back in the model's prose;
  `rehydrate()` restores real values on our machine, after the response.
- **Leak check.** If any placeholder survives into the final draft, that is a
  hard validation failure. `[PHONE_1]` in a sent email is worse than no email.

```python
if red.leaked(draft_text):
    vres = {"pass": False,
            "violations": ["a redaction placeholder survived into the draft"]}
draft_text = red.rehydrate(draft_text)
```

- **The audit log stores the redaction *summary*, never the values.**
  `{'PHONE': 1, 'GSTIN': 1}` tells you the system worked without recording what
  it protected.

### 6.4 What never reaches a model at all

- **Attachments.** Hard stop at Gate 2. NDAs, CIMs and financials never touch a
  provider.
- **Anything caught by the pre-filter.** Bounces, opt-outs, auto-replies and
  read receipts are handled by regex before any API call. Roughly a third of
  inbox volume, and it saves money as well as exposure.
- **Anything flagged sensitive.** Pricing, legal, valuation, press → the message
  is queued. The classification call has already happened by then, which is why
  redaction runs *before* it.

### 6.5 Access, secrets, retention

**Least-privilege OAuth**, and the specific scopes matter in a security review:

```python
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",    # read + send, NOT delete
    "https://www.googleapis.com/auth/calendar.events", # this calendar's events only
]
```

Not `gmail.full`. Not `calendar`. The narrower scope is both safer and much
easier to get approved.

**Secrets:** `.env`, gitignored. Token files written `chmod 600`. `credentials.json`
and `token_*.json` are in `.gitignore` — committing a credential file to a repo
you send a prospective employer ends a process.

**Retention:**

| Store | Contains | Retention |
|---|---|---|
| `audit` | Decisions, confidences, redaction counts. **No bodies** | Indefinite — it's the compliance record |
| `review_queue` | 900 chars of body, because a human needs context to judge | 90 days, encrypted at rest in production |
| `outbox` | Sent drafts | 90 days |
| Redaction map | Nothing. In-memory only | Request lifetime |

**Provider terms:** use an endpoint with zero data retention and no training on
inputs. Name it explicitly in the vendor review — for a firm handling deal flow,
"we sent it to an API" is not an adequate answer to a counterparty asking about
confidentiality.

**Deal figures** are extracted into bands (`revenue_band`, `ebitda_band`) rather
than exact numbers wherever possible. It makes downstream filtering deterministic
*and* reduces the precision of what's stored. Two benefits, one decision.

---

## 7. Summary of what I'd change with more time

In priority order, with reasoning rather than a wish list:

1. **Fill `approved_facts.yaml` with Cicero's real language.** Everything the
   system may say lives there. Reply quality is capped by that file no matter
   how good the prompt is. One hour, highest return available.
2. **Few-shot examples from 5–10 real human replies**, selected by
   `(sender_type, intent)`. Worth more than any prompt tuning for the "sounds
   like a thoughtful team member" requirement.
3. **Broker deal-object extraction and thesis filtering.** If Cicero's reply mix
   is broker-heavy, this is the main system and the founder path is the side
   quest. I built it the other way round and would want to know the mix before
   defending that.
4. **Non-English handling.** In this market Hindi and Hinglish replies are not
   an edge case.
5. **A re-engagement worker** that acts on stored `DEFER` dates. The data is
   already there; nothing fires on it. Probably the highest-conversion email in
   the whole system.
6. **Per-thread locking**, before volume makes concurrent replies a real
   correctness problem rather than a theoretical one.
