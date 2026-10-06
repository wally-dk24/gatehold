# gatehold

An async approval proxy for a personal AI agent's tools.

An agent that needs a human's approval for side-effecting actions usually faces
a bad choice: block until the human appears, or quietly downgrade to doing
nothing. gatehold offers a third path:

1. The agent **proposes** the action: `gatehold run --tool <name> -- <cmd...>`
2. gatehold **simulates** the outcome — from the tool's own `--dry-run`, from a
   declared expected result in the tool's capability profile, or from an
   explicit placeholder — and hands it back clearly labeled **SIMULATED**, so
   the agent can keep planning on provisional data.
3. The real action waits in a local sqlite queue. The human runs
   `gatehold review` later and approves or rejects items one by one or in
   bulk. Approved actions execute for real; rejected ones write a notice the
   originating run can read to replan.

The one hard rule: **an agent can never approve its own queue.** Review
decisions only run on an interactive terminal (a human at a keyboard). There
is no `--yes` flag and no environment override, by design. Listing the queue
is read-only and always allowed.

Inspired by the Gatekeeper pattern in Cloudflare's open-source
[cloudflare-os](https://github.com/cloudflare/cloudflare-os) — speculative
results plus bulk human review — rebuilt as a single-file, stdlib-only tool
for one human and their agent.

## Install

Requires Python 3.12+ (stdlib only — no dependencies).

```bash
git clone https://github.com/wally-dk24/gatehold
cd gatehold
./gatehold.py init        # creates ~/.gatehold (override with GATEHOLD_HOME)
```

Or via Docker (multi-arch: amd64, arm64, 386):

```bash
docker run --rm -v ~/.gatehold:/home/gatehold/.gatehold wallydk24/gatehold:latest run --tool demo -- echo hi
```

## Usage

```bash
# 1. Agent proposes a side-effecting action (simulates, queues, never executes)
gatehold run --tool send-sms --context "cron: inbound-text-check" -- send_sms.py --to +1555... --body "hi"

# 2. Human reviews later, interactively
gatehold review              # per-item: approve (a) / reject (r) / skip (s)
gatehold review --approve <id>
gatehold review --reject <id> --reason "not today"
gatehold review --approve-all   # with confirmation

# 3. Read-only views (safe from any context, including agent runs)
gatehold queue               # pending actions, ages, >24h ESCALATED flags
gatehold report              # printable summary for a daily report
gatehold notices             # rejection notices for originating runs
gatehold notices --action <id>
gatehold profiles            # capability profiles on file
```

## Capability profiles

One YAML file per tool in `~/.gatehold/profiles/<tool>.yaml`:

```yaml
tool: send-sms
description: "Send an SMS via the Twilio helper"
scope:
  - "sms:+1*"
dry_run_append: "--dry-run"          # tier 1: run `<cmd> --dry-run`, use output
expected_result: "SMS queued (simulated)"   # tier 2: declared expectation
rollback: "Cannot unsend; approval required first"
```

Only a small documented YAML subset is supported (key/value, nesting,
`- item` lists) — parsed by ~60 lines of stdlib code, no PyYAML needed.
Reads are never gated; only side effects go through `gatehold run`.

## Simulation tiers

1. The tool's own `--dry-run` output (when the profile declares `dry_run_append`)
2. The profile's declared `expected_result`
3. An explicit `SIMULATED` placeholder

A simulated result is never presented as real — the banner says so on every run.

## Audit

Every state change (`proposed`, `simulated`, `approved`, `rejected`,
`applied`) is appended to `~/.gatehold/audit.jsonl`. Actions pending longer
than 24h are flagged `ESCALATED` in `queue` and `report`.

## Wiring into an agent cron

See `examples/cron-wrapper.sh` for the pattern: wrap the side-effecting call,
continue planning on the simulated result, and let the originating run check
`gatehold notices --action <id>` on its next pass to learn a rejection.

## Design notes

- Single file, stdlib only, ~600 lines including the YAML subset parser.
  Small enough to audit in one sitting.
- The human gate is a TTY check in the review path, not a secret or a flag
  an agent could set. Non-interactive approve/reject attempts are refused.
- The queue, audit log, and notices live under `~/.gatehold`
  (`GATEHOLD_HOME` overrides), protected by normal file permissions.
