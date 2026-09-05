# Cicero Reply Engine

Automated triage, reply and scheduling for the founder/broker reply inbox.

Runs with **zero credentials** out of the box. A mock inbox and a mock calendar
stand in for Google, and a keyword classifier stands in for the LLM, so you can
see the whole system work before setting anything up.

```bash
pip install -r requirements.txt
python make_fixtures.py
python run.py --reset
```

---

## 1. Getting it running

### Step 1 — Python

You need Python 3.10 or newer (`python3 --version`). Nothing else is required
for the demo. No database server, no Docker, no cloud account.

```bash
cd cicero
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2 — the demo, with no credentials at all

```bash
python make_fixtures.py     # writes 16 sample replies into data/fixtures/
python run.py --reset       # processes all of them
python review.py            # work the human queue
python review.py --stats    # edit rates per gate
python -m pytest tests/ -v  # 40 tests
```

Everything works. Nothing leaves your machine.

### Step 3 — turn on the real LLM

```bash
cp .env.example .env
# put your key in ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...
python run.py --reset
```

The header line changes from `llm=MOCK` to `llm=REAL`. Nothing else changes —
that is the point of keeping the model behind one file.

### Step 4 — connect Gmail and Calendar

Full instructions are in the docstring at the top of `authorize.py`. Summary:
create a Google Cloud project, enable the Gmail and Calendar APIs, create a
**Desktop app** OAuth client, download `credentials.json`, then:

```bash
python authorize.py         # opens a browser once
python run.py --live        # reads real mail, books real events, sends NOTHING
python run.py --live --send # actually sends. Read the queue first.
python run.py --flush       # deliver anything whose delay window has expired
```

`--live` without `--send` is the mode you should sit in for the first week.

### Step 5 — running it continuously

For a prototype, cron is the right answer. Not Kubernetes, not Airflow.

```cron
*/2 * * * * cd /path/to/cicero && .venv/bin/python run.py --live >> logs/run.log 2>&1
*/1 * * * * cd /path/to/cicero && .venv/bin/python run.py --flush --send >> logs/send.log 2>&1
```

Two separate jobs on purpose. Processing and sending are decoupled, so the
delay window is real and a human can still cancel between the two.

---

## 2. Why this stack

| Choice | Why | What I'd use at scale |
|---|---|---|
| Plain Python scripts | Readable top to bottom. No framework to learn. | Same code, wrapped in a queue worker |
| SQLite | One file, zero setup, but real transactions and UNIQUE constraints — which is what actually prevents double-sends | Postgres |
| Cron | Two lines, and it is obvious what runs when | Gmail `watch()` → Pub/Sub push |
| Gmail API | Only way to send in-thread from a specific alias with correct headers | Same |
| Google Calendar API | FreeBusy is the only reliable double-booking guard | Same |
| YAML config | Business rules change weekly; code changes shouldn't | Same, in version control |
| CLI review queue | The interesting problem is *where* the human sits, not the UI | Web app or Slack |

Everything deliberately left out for the prototype is listed in section 5 below.

---

## 3. The eleven stages

```
 0  dedupe on Message-ID                                  store.py
 1  fetch from the primary inbox                          gmail_io.py    [Gmail API]
 2  resolve identity: real sender + owning mailbox        identity.py
 3  filter non-replies: OOO, bounces, opt-outs, invites   prefilter.py
 4  load contact, thread state, commitment ledger         store.py
 5  SUPPRESS THE SEQUENCER  (first write, always)         store.py
 6  extract intent, entities, questions                   llm.py         [LLM call 1]
 6b fact coverage check                                   facts.py
 7  route: confidence vector -> allowlist                 router.py
 8  pick a slot, CREATE THE EVENT                         scheduler.py   [Calendar API]
 9  draft the reply from real facts                       llm.py         [LLM call 2]
10  validate: rules, then a critic                        validator.py   [LLM call 3]
11  send after a jittered delay, in-thread                gmail_io.py    [Gmail API]
```

Three ordering choices carry the whole argument:

- **Stage 5 runs before stage 6.** Sequence suppression happens before any
  reasoning, so a follow-up cannot land after they replied even if every later
  stage crashes.
- **Stage 6b runs before stage 9.** We check what we are allowed to say before
  we generate anything.
- **Stage 8 runs before stage 9.** The event exists before the email describing
  it is written. If sending later fails, a compensating cancel runs.

---

## 4. The six required answers

They are answered in full, with worked examples and the verbatim text of every
prompt, in **`DESIGN.md`**. That document is the design rationale; this one is
the operating manual.

Short version:

| Question | One-line answer | Detail |
|---|---|---|
| 1. Intent classification | Founder/broker is a DB lookup, not a model call. Intents are defined by the action they cause. | DESIGN.md §1 |
| 2. Reply quality | The model is only given the facts it may state; three independent layers check the output. | DESIGN.md §2 |
| 3. Scheduling | The event is created *before* the email is written. A pure function picks the time. | DESIGN.md §3 |
| 4. Guardrails | Confidence is a five-signal vector, only one from the model. Four gates. The line moves on measured edit rates. | DESIGN.md §4 |
| 5. Architecture | Eleven stages; the LLM reads and writes but never decides. Cuts named explicitly. | DESIGN.md §5 |
| 6. Data handling | Selective redaction — names stay for tone, everything sensitive goes. | DESIGN.md §6 |

## 5. Files

```
config/policy.yaml          every business rule and threshold
config/brand_voice.yaml     tone rules and prohibitions
config/approved_facts.yaml  the only claims the model may make  <- START HERE
config/contacts.csv         sender_type ground truth
src/models.py               data shapes
src/store.py                SQLite: contacts, threads, ledger, queues, audit
src/identity.py             stage 2 -- unwinding Gmail forwarding
src/prefilter.py            stage 3 -- non-replies, before any LLM call
src/llm.py                  the only file that talks to a model
src/redact.py               PII redaction before anything leaves the machine
src/facts.py                fact coverage -- the anti-hallucination check
src/router.py               stage 7 -- every send/hold decision
src/scheduler.py            pure slot picker + calendar clients
src/validator.py            10 deterministic checks on the draft
src/gmail_io.py             fetch and send, with correct threading
src/pipeline.py             the orchestrator -- read this one first
run.py                      demo entry point
review.py                   the human queue
authorize.py                one-time Google OAuth
make_fixtures.py            the 16-message mock inbox
tests/test_core.py          40 tests on the riskiest functions
```

## 6. What to do first

1. Fill in `config/approved_facts.yaml` with Cicero's real language. Everything
   the system is allowed to say lives there, and it is currently placeholder
   text. This is the highest-value hour available.
2. Replace the fixtures in `data/fixtures/` with real anonymised replies, and
   add an `expect_route` field to each. That set is your eval.
3. Collect 5–10 replies a human at Cicero actually wrote and put them into
   `llm.py` as few-shot examples, selected by `(sender_type, intent)`. This
   matters more than any amount of prompt tuning.
