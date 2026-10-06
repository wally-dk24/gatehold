#!/usr/bin/env python3
"""gatehold — async approval proxy for a personal AI agent's tools.

An agent that needs a human's approval for side-effecting actions usually
faces a bad choice: block until the human appears, or quietly downgrade to
doing nothing. gatehold offers a third path: the agent proposes the action,
gatehold SIMULATES the outcome so the agent can keep planning on provisional
data, and the real action waits in a queue for the human to approve or reject
in bulk later.

The one hard rule: an agent can never approve its own queue. Review decisions
(`approve` / `reject`) only run on an interactive terminal (a human at a
keyboard). There is no --yes flag and no environment override, by design.

Data lives under ~/.gatehold/ (override with GATEHOLD_HOME):
  queue.db      sqlite queue of proposed actions
  audit.jsonl   append-only log of every state change
  profiles/     per-tool YAML capability profiles
  notices/      rejection notices the originating run can read to replan

Only the Python standard library is used.
"""

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import uuid

VERSION = "0.1.0"
ESCALATION_SECONDS = 24 * 3600
SIM_OUTPUT_LIMIT = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  argv_json TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  simulated_result TEXT NOT NULL DEFAULT '',
  simulation_source TEXT NOT NULL DEFAULT '',
  decided_at TEXT,
  decided_by TEXT DEFAULT 'human',
  reject_reason TEXT,
  executed_at TEXT,
  exit_code INTEGER,
  output TEXT
);
"""


# --------------------------------------------------------------------------
# paths & storage
# --------------------------------------------------------------------------

def gatehold_home():
    return os.environ.get("GATEHOLD_HOME",
                          os.path.join(os.path.expanduser("~"), ".gatehold"))


def _ensure_dirs():
    base = gatehold_home()
    for sub in ("", "profiles", "notices"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    return base


def _db():
    base = _ensure_dirs()
    conn = sqlite3.connect(os.path.join(base, "queue.db"))
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def _audit(event, action_id=None, **details):
    base = _ensure_dirs()
    record = {"ts": _utcnow(), "event": event}
    if action_id:
        record["action_id"] = action_id
    record.update(details)
    with open(os.path.join(base, "audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _age_seconds(created_at):
    try:
        dt = datetime.datetime.fromisoformat(created_at)
    except ValueError:
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0, int((now - dt).total_seconds()))


def _fmt_age(seconds):
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


# --------------------------------------------------------------------------
# minimal YAML subset parser (documented; profiles only need this much)
#
# Supported:
#   - full-line comments starting with '#'
#   - `key: value` pairs (values may be 'single' or "double" quoted)
#   - nested mappings via indentation (spaces, no tabs)
#   - lists via `- item` lines under a key
# Anything else raises ValueError. '#' inside a value is literal.
# --------------------------------------------------------------------------

def _unquote(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return s


def parse_simple_yaml(text):
    root = {}
    # stack entries: [indent, container, parent_container, key_in_parent]
    stack = [[-1, root, None, None]]
    for lineno, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent]:
            raise ValueError(f"line {lineno}: tabs not allowed, use spaces")
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        _ip, container, parent, key_in_parent = stack[-1]
        if s.startswith("- "):
            if isinstance(container, dict):
                if container:
                    raise ValueError(
                        f"line {lineno}: list item inside a non-empty mapping")
                lst = []
                parent[key_in_parent] = lst
                stack[-1][1] = lst
                container = lst
            if not isinstance(container, list):
                raise ValueError(f"line {lineno}: list item outside a list")
            container.append(_unquote(s[2:].strip()))
        elif ":" in s:
            if isinstance(container, list):
                raise ValueError(f"line {lineno}: mapping entry inside a list")
            key, _, val = s.partition(":")
            key = key.strip()
            val = val.strip()
            if not key:
                raise ValueError(f"line {lineno}: empty key")
            if val:
                container[key] = _unquote(val)
            else:
                child = {}
                container[key] = child
                stack.append([indent, child, container, key])
        else:
            raise ValueError(f"line {lineno}: cannot parse: {s!r}")
    return root


def load_profile(tool):
    path = os.path.join(gatehold_home(), "profiles", tool + ".yaml")
    if not os.path.exists(path):
        return None, path
    with open(path, encoding="utf-8") as f:
        return parse_simple_yaml(f.read()), path


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------

def simulate(tool, argv, profile):
    """Return (simulated_text, source). Never executes the real action."""
    # Tier 1: the tool's own dry-run output.
    dry_flag = (profile or {}).get("dry_run_append")
    if dry_flag:
        try:
            proc = subprocess.run(
                list(argv) + [dry_flag],
                capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                out = (proc.stdout or "").strip() or "(dry-run produced no output)"
                return out[:SIM_OUTPUT_LIMIT], "dry-run"
        except (OSError, subprocess.TimeoutExpired):
            pass  # fall through to the next tier
    # Tier 2: declared expected result from the capability profile.
    expected = (profile or {}).get("expected_result")
    if expected:
        return str(expected), "profile"
    # Tier 3: explicit placeholder. Never presented as real.
    return (f"SIMULATED: `{tool}` {' '.join(argv)} — no dry-run or expected "
            f"result declared; treat as provisional."), "placeholder"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(_args):
    base = _ensure_dirs()
    example = os.path.join(base, "profiles", "example.yaml")
    if not os.path.exists(example):
        with open(example, "w", encoding="utf-8") as f:
            f.write(
                "# Example capability profile for gatehold.\n"
                "# Only this small YAML subset is supported: key: value pairs,\n"
                "# nested mappings via indentation, and '- item' lists.\n"
                "tool: example\n"
                "description: \"What this tool does, in one line.\"\n"
                "scope:\n"
                "  - \"what it may touch, e.g. sms:+1*\"\n"
                "dry_run_append: \"--dry-run\"\n"
                "expected_result: \"What the agent should assume on simulation.\"\n"
                "rollback: \"How to undo it, or 'irreversible'.\"\n"
            )
    print(f"gatehold {VERSION} initialized at {base}")
    print("profiles/  put one <tool>.yaml per side-effecting tool here")
    print("queue.db   sqlite queue of pending actions")
    print("audit.jsonl append-only audit log")


def cmd_run(args):
    if not args.argv:
        print("error: no command given after --", file=sys.stderr)
        return 2
    profile, profile_path = load_profile(args.tool)
    if profile is None:
        print(f"note: no profile at {profile_path}; "
              f"using placeholder simulation", file=sys.stderr)
    simulated, source = simulate(args.tool, args.argv, profile)
    action_id = uuid.uuid4().hex[:12]
    created = _utcnow()
    conn = _db()
    conn.execute(
        "INSERT INTO actions (id, tool, argv_json, context, status, created_at,"
        " simulated_result, simulation_source) VALUES (?,?,?,?,?,?,?,?)",
        (action_id, args.tool, json.dumps(args.argv), args.context or "",
         "pending", created, simulated, source))
    conn.commit()
    conn.close()
    _audit("proposed", action_id, tool=args.tool, argv=args.argv,
           context=args.context or "")
    _audit("simulated", action_id, source=source)
    print("=" * 62)
    print("SIMULATED RESULT — gatehold. This action did NOT execute.")
    print(f"action: {action_id}   tool: {args.tool}   source: {source}")
    print("=" * 62)
    print(simulated)
    print("=" * 62)
    print("Queued for human review. Run `gatehold review` to decide.")
    return 0


def _pending_rows(conn):
    cur = conn.execute(
        "SELECT id, tool, argv_json, context, created_at, simulated_result"
        " FROM actions WHERE status='pending' ORDER BY created_at")
    return cur.fetchall()


def _print_queue(rows):
    if not rows:
        print("Queue empty — nothing pending.")
        return
    for i, (aid, tool, argv_json, context, created, sim) in enumerate(rows, 1):
        age = _age_seconds(created)
        flag = "  [ESCALATED: pending >24h]" if age > ESCALATION_SECONDS else ""
        argv = json.loads(argv_json)
        print(f"[{i}] {aid}  {tool}  age {_fmt_age(age)}{flag}")
        print(f"    cmd: {' '.join(argv)}")
        if context:
            print(f"    context: {context}")
        preview = sim.replace("\n", " ")[:160]
        print(f"    simulated: {preview}")


def _require_human():
    if not sys.stdin.isatty():
        print("error: review decisions require an interactive terminal "
              "(human-only).", file=sys.stderr)
        print("Refusing to approve or reject from a non-interactive session. "
              "Listing the queue is read-only and always allowed.",
              file=sys.stderr)
        return False
    return True


def _apply_decision(action_id, decision, reason=""):
    """Execute one human decision. Called only from an interactive review."""
    conn = _db()
    row = conn.execute(
        "SELECT tool, argv_json, context FROM actions WHERE id=? AND status='pending'",
        (action_id,)).fetchone()
    if not row:
        conn.close()
        print(f"no pending action {action_id}", file=sys.stderr)
        return 1
    tool, argv_json, _context = row
    argv = json.loads(argv_json)
    decided = _utcnow()
    if decision == "approve":
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=600)
            rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as e:
            rc, out = 124, f"execution failed: {e}"
        conn.execute(
            "UPDATE actions SET status='approved', decided_at=?,"
            " executed_at=?, exit_code=?, output=? WHERE id=?",
            (decided, decided, rc, out[:20000], action_id))
        conn.commit()
        conn.close()
        _audit("approved", action_id, tool=tool, decided_by="human")
        _audit("applied", action_id, exit_code=rc)
        print("-" * 62)
        print(f"REAL EXECUTION — approved by human. exit={rc}")
        print("-" * 62)
        print(out[:SIM_OUTPUT_LIMIT])
        return 0
    else:
        conn.execute(
            "UPDATE actions SET status='rejected', decided_at=?,"
            " reject_reason=? WHERE id=?",
            (decided, reason, action_id))
        conn.commit()
        conn.close()
        notice = {
            "action_id": action_id,
            "tool": tool,
            "argv": argv,
            "rejected_at": decided,
            "reason": reason,
            "guidance": "The proposed action was rejected by the human. "
                        "Replan without it; do not retry silently.",
        }
        npath = os.path.join(_ensure_dirs(), "notices", action_id + ".json")
        with open(npath, "w", encoding="utf-8") as f:
            json.dump(notice, f, indent=2)
        _audit("rejected", action_id, tool=tool, reason=reason,
               notice=npath)
        print(f"rejected {action_id}; notice written to {npath}")
        return 0


def cmd_review(args):
    conn = _db()
    rows = _pending_rows(conn)
    conn.close()
    wants_decision = bool(args.approve or args.reject or args.approve_all
                          or args.reject_all or args.interactive)
    if wants_decision and not _require_human():
        return 2
    if args.approve:
        return _apply_decision(args.approve, "approve")
    if args.reject:
        return _apply_decision(args.reject, "reject", args.reason or "")
    if args.approve_all:
        if not rows:
            print("nothing to approve")
            return 0
        confirm = input(f"approve ALL {len(rows)} pending actions? [y/N] ")
        if confirm.strip().lower() != "y":
            print("aborted")
            return 0
        for (aid, *_rest) in rows:
            _apply_decision(aid, "approve")
        return 0
    if args.reject_all:
        if not rows:
            print("nothing to reject")
            return 0
        confirm = input(f"reject ALL {len(rows)} pending actions? [y/N] ")
        if confirm.strip().lower() != "y":
            print("aborted")
            return 0
        for (aid, *_rest) in rows:
            _apply_decision(aid, "reject", args.reason or "")
        return 0
    if not rows:
        print("Queue empty — nothing pending.")
        return 0
    if not sys.stdin.isatty():
        _print_queue(rows)
        return 0
    # interactive per-item review
    for i, (aid, tool, argv_json, context, created, sim) in enumerate(rows, 1):
        age = _age_seconds(created)
        flag = "  [ESCALATED: pending >24h]" if age > ESCALATION_SECONDS else ""
        argv = json.loads(argv_json)
        print(f"\n[{i}/{len(rows)}] {aid}  {tool}  age {_fmt_age(age)}{flag}")
        print(f"  cmd: {' '.join(argv)}")
        if context:
            print(f"  context: {context}")
        print(f"  simulated: {sim[:400]}")
        while True:
            ans = input("  approve (a) / reject (r) / skip (s) / quit (q)? "
                        ).strip().lower()
            if ans in ("a", "r", "s", "q"):
                break
            print("  please answer a, r, s, or q")
        if ans == "q":
            print("stopped; remaining items stay queued")
            return 0
        if ans == "s":
            continue
        if ans == "r":
            reason = input("  reason (optional): ").strip()
            _apply_decision(aid, "reject", reason)
        else:
            _apply_decision(aid, "approve")
    return 0


def cmd_queue(_args):
    conn = _db()
    rows = _pending_rows(conn)
    conn.close()
    _print_queue(rows)
    return 0


def cmd_report(_args):
    conn = _db()
    rows = _pending_rows(conn)
    total = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    done = conn.execute(
        "SELECT status, COUNT(*) FROM actions GROUP BY status").fetchall()
    conn.close()
    esc = sum(1 for r in rows if _age_seconds(r[4]) > ESCALATION_SECONDS)
    print("# gatehold queue report")
    print(f"- pending: {len(rows)} ({esc} escalated >24h)")
    print(f"- lifetime actions: {total} "
          + ", ".join(f"{s}: {c}" for s, c in done))
    for (aid, tool, argv_json, context, created, _sim) in rows:
        age = _age_seconds(created)
        flag = " ESCALATED" if age > ESCALATION_SECONDS else ""
        print(f"- [{aid}] {tool} `{' '.join(json.loads(argv_json))}`"
              f" age {_fmt_age(age)}{flag}")
    return 0


def cmd_notices(args):
    ndir = os.path.join(_ensure_dirs(), "notices")
    if args.action:
        path = os.path.join(ndir, args.action + ".json")
        if not os.path.exists(path):
            print(f"no notice for action {args.action}")
            return 1
        with open(path, encoding="utf-8") as f:
            print(f.read())
        return 0
    files = sorted(f for f in os.listdir(ndir) if f.endswith(".json"))
    if not files:
        print("no rejection notices")
        return 0
    for fn in files:
        with open(os.path.join(ndir, fn), encoding="utf-8") as f:
            n = json.load(f)
        print(f"{n['action_id']}  {n['tool']}  rejected {n['rejected_at']}"
              f"  reason: {n.get('reason') or '(none)'}")
    return 0


def cmd_profiles(_args):
    pdir = os.path.join(_ensure_dirs(), "profiles")
    files = sorted(f for f in os.listdir(pdir) if f.endswith(".yaml"))
    if not files:
        print("no profiles; run `gatehold init` for an example")
        return 0
    for fn in files:
        try:
            with open(os.path.join(pdir, fn), encoding="utf-8") as f:
                prof = parse_simple_yaml(f.read())
            desc = prof.get("description", "")
            print(f"{fn[:-5]:20s} {desc}")
        except ValueError as e:
            print(f"{fn[:-5]:20s} INVALID: {e}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="gatehold",
        description="Async approval proxy: propose now, simulate, "
                    "let a human decide later.")
    ap.add_argument("--version", action="version", version=f"gatehold {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create ~/.gatehold layout + example profile")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="propose a side-effecting action (simulates, queues)")
    p.add_argument("--tool", required=True, help="capability profile name")
    p.add_argument("--context", default="", help="run context for the audit log")
    p.add_argument("argv", nargs=argparse.REMAINDER,
                   help="use -- to separate: gatehold run --tool t -- CMD...")
    # argparse puts everything after -- into argv, including the '--' itself
    # when nargs=REMAINDER is used after an option; strip a leading '--'.
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("review", help="human review of the pending queue")
    p.add_argument("--approve", metavar="ID", help="approve one action (tty only)")
    p.add_argument("--reject", metavar="ID", help="reject one action (tty only)")
    p.add_argument("--reason", default="", help="rejection reason")
    p.add_argument("--approve-all", action="store_true")
    p.add_argument("--reject-all", action="store_true")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="force interactive per-item review")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("queue", help="list pending actions (read-only)")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("report", help="printable queue summary for the daily report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("notices", help="read rejection notices")
    p.add_argument("--action", metavar="ID", help="show one notice")
    p.set_defaults(func=cmd_notices)

    p = sub.add_parser("profiles", help="list capability profiles")
    p.set_defaults(func=cmd_profiles)

    args = ap.parse_args(argv)
    if args.cmd == "run" and args.argv[:1] == ["--"]:
        args.argv = args.argv[1:]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
