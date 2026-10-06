#!/bin/bash
# gatehold dogfood verification: propose -> simulate -> queue -> guard -> decide -> audit
set -u
export GATEHOLD_HOME=/tmp/gatehold-test
rm -rf "$GATEHOLD_HOME"
G=./gatehold.py
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $1"; }

$G init >/dev/null || bad "init"

# profile with expected_result (tier 2)
cat > "$GATEHOLD_HOME/profiles/demo.yaml" <<'EOF'
tool: demo
description: "Demo side-effecting tool"
scope:
  - "files:/tmp/gatehold-test/*"
expected_result: "SIMULATED-BY-PROFILE: proof file would be created"
rollback: "rm the file"
EOF

# 1. propose + simulate (tier 2: profile expected_result)
OUT=$($G run --tool demo --context "dogfood test" -- touch /tmp/gatehold-test/proof.txt)
echo "$OUT" | grep -q "SIMULATED RESULT" || bad "simulated banner"
echo "$OUT" | grep -q "SIMULATED-BY-PROFILE" || bad "tier2 simulation"
echo "$OUT" | grep -q "did NOT execute" || bad "not-executed label"
[ -f /tmp/gatehold-test/proof.txt ] && bad "real action must not execute on run"
AID=$(echo "$OUT" | grep -oP '^action: \K[0-9a-f]{12}' | head -1)
[ -n "$AID" ] || bad "action id parsed"
ok "run: simulated, queued, not executed (id=$AID)"

# 2. queue shows pending
$G queue | grep -q "$AID" || bad "queue lists action"
ok "queue: pending visible"

# 3. audit has proposed + simulated
grep -q '"event": "proposed"' "$GATEHOLD_HOME/audit.jsonl" || bad "audit proposed"
grep -q '"event": "simulated"' "$GATEHOLD_HOME/audit.jsonl" || bad "audit simulated"
ok "audit: proposed + simulated logged"

# 4. GUARD: non-interactive approve must be refused (stdin is not a tty here)
if echo | $G review --approve "$AID" 2>/dev/null; then bad "guard: non-tty approve allowed"; else ok "guard: non-tty approve refused"; fi
sqlite3 "$GATEHOLD_HOME/queue.db" "SELECT status FROM actions WHERE id='$AID';" | grep -q pending || bad "guard: status changed"

# 5. decision mechanics via the human-gated function (same code path review uses).
#    We call _apply_decision directly here as the test harness standing in for
#    the interactive terminal; the TTY gate itself was verified in step 4.
python3 - "$AID" <<'EOF'
import sys
sys.path.insert(0, ".")
import gatehold
rc = gatehold._apply_decision(sys.argv[1], "approve")
sys.exit(rc)
EOF
[ -f /tmp/gatehold-test/proof.txt ] || bad "approve: real action executed"
sqlite3 "$GATEHOLD_HOME/queue.db" "SELECT status FROM actions WHERE id='$AID';" | grep -q approved || bad "approve: status"
grep -q '"event": "applied"' "$GATEHOLD_HOME/audit.jsonl" || bad "audit applied"
ok "decide: approve executes real action, audit applied"

# 6. reject path writes a notice the originating run can read
OUT2=$($G run --tool demo -- touch /tmp/gatehold-test/never.txt)
AID2=$(echo "$OUT2" | grep -oP '^action: \K[0-9a-f]{12}' | head -1)
python3 - "$AID2" <<'EOF'
import sys
sys.path.insert(0, ".")
import gatehold
sys.exit(gatehold._apply_decision(sys.argv[1], "reject", "test rejection"))
EOF
[ -f /tmp/gatehold-test/never.txt ] && bad "reject: action executed"
[ -f "$GATEHOLD_HOME/notices/$AID2.json" ] || bad "reject: notice file"
$G notices --action "$AID2" | grep -q "test rejection" || bad "reject: notice readable"
grep -q '"event": "rejected"' "$GATEHOLD_HOME/audit.jsonl" || bad "audit rejected"
ok "decide: reject skips execution, writes notice, audit rejected"

# 7. escalation flag for >24h pending
sqlite3 "$GATEHOLD_HOME/queue.db" "INSERT INTO actions (id,tool,argv_json,status,created_at,simulated_result,simulation_source) VALUES ('old000000001','demo','[\"x\"]','pending','2020-01-01T00:00:00+00:00','s','placeholder');"
$G queue | grep -q "ESCALATED" || bad "escalation flag"
$G report | grep -q "escalated" || bad "report escalation"
ok "escalation: >24h pending flagged in queue + report"

# 8. tier 3 placeholder when no profile
OUT3=$($G run --tool nosuchprofile -- /bin/true)
echo "$OUT3" | grep -q "no dry-run or expected result declared" || bad "tier3 placeholder"
ok "simulation tier 3: placeholder for unknown tool"

# 9. tier 1: tool's own --dry-run
mkdir -p "$GATEHOLD_HOME/profiles"
cat > "$GATEHOLD_HOME/fakebin.sh" <<'EOF'
#!/bin/bash
if [ "$1" = "--dry-run" ]; then echo "DRYRUN-OK: would frobnicate"; exit 0; fi
echo "REAL RUN"; exit 0
EOF
chmod +x "$GATEHOLD_HOME/fakebin.sh"
cat > "$GATEHOLD_HOME/profiles/fake.yaml" <<'EOF'
tool: fake
description: "fake tool"
dry_run_append: "--dry-run"
EOF
OUT4=$($G run --tool fake -- "$GATEHOLD_HOME/fakebin.sh")
echo "$OUT4" | grep -q "DRYRUN-OK" || bad "tier1 dry-run"
echo "$OUT4" | grep -q "source: dry-run" || bad "tier1 source label"
ok "simulation tier 1: tool dry-run output used"

echo "----"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
