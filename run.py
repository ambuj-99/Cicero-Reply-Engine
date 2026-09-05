#!/usr/bin/env python3
"""
run.py -- the single command that demonstrates the whole system.

  python run.py                 # mock inbox, mock calendar, nothing sent
  python run.py --live          # real Gmail + real Calendar, still no send
  python run.py --live --send   # actually sends. Be sure.
  python run.py --flush         # send anything whose delay has expired
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.pipeline import Pipeline
from src.store import Store
from src import llm

C = {"g": "\033[92m", "y": "\033[93m", "r": "\033[91m", "b": "\033[94m",
     "d": "\033[90m", "x": "\033[0m", "B": "\033[1m"}

# Colour codes only work in a real terminal. Notebooks and piped output render
# them as literal garbage, so switch them off unless stdout is a TTY.
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def colour_for(action):
    if action.startswith("AUTO_SEND"): return C["g"]
    if action.startswith("QUEUED"): return C["y"]
    if action in ("NO_ACTION", "SKIPPED_DUPLICATE"): return C["d"]
    return C["r"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use real Google APIs")
    ap.add_argument("--send", action="store_true", help="actually deliver mail")
    ap.add_argument("--flush", action="store_true", help="flush the outbox and exit")
    ap.add_argument("--reset", action="store_true", help="wipe the database first")
    args = ap.parse_args()

    if args.reset and os.path.exists("data/store.db"):
        os.remove("data/store.db")

    pipe = Pipeline(mock=not args.live, dry_run=not args.send)
    pipe.store.load_contacts_csv()

    if args.flush:
        n = pipe.flush_outbox()
        print(f"flushed {n} messages")
        return

    mode = "LIVE" if args.live else "MOCK"
    brain = "MOCK (no API key set)" if llm.USE_MOCK else f"REAL ({llm.MODEL})"
    print(f"\n{C['B']}CICERO REPLY ENGINE{C['x']}   inbox={mode}  llm={brain}  "
          f"delivery={'ON' if args.send else 'DRY RUN'}")
    print("=" * 100)

    outcomes = pipe.run_once()
    counts = {}

    for o in outcomes:
        counts[o.action] = counts.get(o.action, 0) + 1
        i, e, d = o.identity, o.extraction, o.decision
        col = colour_for(o.action)

        print(f"\n{C['B']}{o.message_id}{C['x']}")
        if i:
            print(f"  {C['d']}identity  {C['x']}{i.real_sender}  "
                  f"via={i.method} conf={i.confidence}  mailbox={i.outbound_mailbox}")
            for n in i.notes:
                print(f"            {C['d']}- {n}{C['x']}")
        if e:
            sec = f" +{e.secondary_intents}" if e.secondary_intents else ""
            print(f"  {C['d']}intent    {C['x']}{e.action_intent}{sec}  conf={e.confidence}  "
                  f"flags={e.sensitive_flags or '-'}  questions={len(e.open_questions)}")
            if e.defer_subtype not in ("UNKNOWN", ""):
                print(f"  {C['d']}defer     {C['x']}{e.defer_subtype}  hint='{e.reengage_hint}'")
        if d:
            cv = d.confidence
            print(f"  {C['d']}confidence{C['x']} id={cv.identity} intent={cv.intent} "
                  f"facts={cv.fact_coverage} sched={cv.schedule} state={cv.state}  "
                  f"-> binding={C['B']}{d.binding_constraint}{C['x']}")
            print(f"  {C['d']}route     {col}{d.route}{C['x']}  "
                  f"{d.gate}  :: {'; '.join(d.reasons)}")
        if o.event:
            print(f"  {C['d']}calendar  {C['g']}{o.event.event_id}{C['x']}  {o.event.human_time}")
        if o.draft:
            ok = C['g'] + 'PASS' + C['x'] if o.validation.get("pass") else \
                 C['r'] + 'FAIL' + C['x']
            print(f"  {C['d']}validate  {C['x']}{ok}  {o.validation.get('violations') or ''}")
            for line in o.draft.split("\n"):
                if line.strip():
                    print(f"  {C['d']}draft     {C['x']}{line.strip()[:110]}")
        if o.error:
            print(f"  {C['r']}error     {o.error}{C['x']}")
        print(f"  {C['d']}action    {col}{C['B']}{o.action}{C['x']}")

    print("\n" + "=" * 100)
    print(f"{C['B']}SUMMARY{C['x']}  {len(outcomes)} messages")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {colour_for(k)}{v:>3}{C['x']}  {k}")

    pending = len(pipe.store.pending_reviews())
    auto = sum(v for k, v in counts.items() if k.startswith("AUTO_SEND"))
    print(f"\n  automated: {auto}/{len(outcomes)}   awaiting a human: {pending}")
    print(f"  review the queue with:  python review.py\n")


if __name__ == "__main__":
    main()
