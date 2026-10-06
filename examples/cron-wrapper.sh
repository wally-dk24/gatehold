#!/bin/bash
# examples/cron-wrapper.sh — how an agent cron proposes a side-effecting
# action through gatehold instead of blocking on approval or doing nothing.
#
# Pattern:
#   1. Propose via `gatehold run`. It prints a SIMULATED result; the real
#      action is queued for the human's later `gatehold review`.
#   2. Continue the run's planning on the provisional (simulated) data.
#   3. On the next pass, check `gatehold notices --action <id>`: if the
#      human rejected it, replan without it instead of retrying silently.
set -u

TOOL="${1:?usage: cron-wrapper.sh <tool> [--context text] -- <cmd...>}"

OUT=$(gatehold run --tool "$TOOL" --context "cron: $(basename "$0")" -- "${@:2}")
ACTION_ID=$(echo "$OUT" | grep -oP '^action: \K[0-9a-f]{12}' | head -1)

echo "$OUT"   # the SIMULATED result the agent plans onward with
echo "ACTION_ID=$ACTION_ID"

# ... agent continues its run on the simulated result ...
#
# Next pass (e.g. tomorrow's cron run):
#   if gatehold notices --action "$ACTION_ID" >/dev/null 2>&1; then
#     echo "human rejected $ACTION_ID — replanning without it"
#   fi
