#!/usr/bin/env python3
"""
review.py -- the human in the loop.

Deliberately a terminal tool, not a web app. For a prototype, a web UI is
effort spent on the least interesting part of the problem.

The thing that matters here is the EDIT LOG. Every time a human rewrites a
draft, we store the original and the edit. Over time, the edit rate for each
(thread_state, intent) pair tells you which cells are safe to promote onto
the auto-send allowlist. That is how the human boundary moves on evidence
instead of on opinion.
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.store import Store
from src.pipeline import Pipeline

C = {"g": "\033[92m", "y": "\033[93m", "r": "\033[91m", "d": "\033[90m",
     "x": "\033[0m", "B": "\033[1m"}

# Colour codes only work in a real terminal. Notebooks and piped output render
# them as literal garbage, so switch them off unless stdout is a TTY.
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def stats(store):
    rows = store.conn.execute(
        "SELECT gate, status, COUNT(*) n FROM review_queue GROUP BY gate, status").fetchall()
    print(f"\n{C['B']}EDIT RATES BY GATE{C['x']}  (drives allowlist widening)")
    agg = {}
    for r in rows:
        g = agg.setdefault(r["gate"] or "-", {"APPROVED": 0, "EDITED": 0, "REJECTED": 0, "PENDING": 0})
        g[r["status"]] = g.get(r["status"], 0) + r["n"]
    for gate, d in sorted(agg.items()):
        done = d["APPROVED"] + d["EDITED"] + d["REJECTED"]
        rate = (d["EDITED"] + d["REJECTED"]) / done if done else 0
        flag = f"{C['g']} <- safe to automate{C['x']}" if done >= 20 and rate < 0.10 else ""
        print(f"  {gate:<10} resolved={done:<4} edit/reject rate={rate:.0%}{flag}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    store = Store()
    if args.stats:
        return stats(store)

    pending = store.pending_reviews()
    if not pending:
        print("queue is empty")
        return stats(store)

    pipe = Pipeline(mock=True, dry_run=True)

    for row in pending:
        p = json.loads(row["payload"])
        print("\n" + "=" * 90)
        print(f"{C['B']}#{row['id']}  from {p.get('from')}{C['x']}   "
              f"{C['y']}{row['gate']}{C['x']}  binding={row['binding_constraint']}")
        print(f"{C['d']}intent:{C['x']} {p.get('intent')}")
        print(f"{C['d']}why held:{C['x']} {'; '.join(p.get('reasons') or [])}")
        if p.get("event"):
            print(f"{C['d']}calendar:{C['x']} {p['event']}")
        print(f"\n{C['d']}--- their message ---{C['x']}\n{(p.get('body') or '')[:700]}")
        if p.get("draft"):
            print(f"\n{C['g']}--- proposed reply ---{C['x']}\n{p['draft']}")
        else:
            print(f"\n{C['r']}--- no draft was generated ---{C['x']}")

        choice = input(f"\n[{C['g']}a{C['x']}]pprove  [{C['y']}e{C['x']}]dit  "
                       f"[{C['r']}r{C['x']}]eject  [s]kip  [q]uit > ").strip().lower()

        if choice == "q":
            break
        if choice == "s":
            continue
        if choice == "a" and p.get("draft"):
            store.resolve_review(row["id"], "APPROVED")
            _send(pipe, store, row, p, p["draft"])
        elif choice == "e":
            print("paste the corrected reply, blank line to finish:")
            lines = []
            while True:
                ln = input()
                if not ln.strip():
                    break
                lines.append(ln)
            new = "\n".join(lines)
            # storing the ORIGINAL alongside the edit is what makes this data
            store.resolve_review(row["id"], "EDITED",
                                 json.dumps({"was": p.get("draft"), "now": new}))
            _send(pipe, store, row, p, new)
        else:
            store.resolve_review(row["id"], "REJECTED")

        # a human touched the thread: reset the machine-turn counter
        t = store.get_thread(row["thread_key"])
        if t:
            t.machine_turns = 0
            t.last_human_touch = row["created_at"]
            store.upsert_thread(t)

    stats(store)


def _send(pipe, store, row, p, body):
    t = store.get_thread(row["thread_key"])
    store.enqueue_send(thread_key=row["thread_key"], to_email=p["from"],
                       from_mailbox=(t.outbound_mailbox if t else "team@cicero.com"),
                       subject="Re: (thread)", body=body,
                       in_reply_to=row["message_id"], references_hdr="",
                       send_after=store.conn.execute("SELECT datetime('now')").fetchone()[0])
    print(f"{C['g']}queued for delivery{C['x']}")


if __name__ == "__main__":
    main()
