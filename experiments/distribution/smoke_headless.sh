#!/usr/bin/env bash
# smoke_headless.sh — experiment: the headless runtime has no Textual, and its three heads still run.
#
# Usage: smoke_headless.sh <unpacked headless OUT dir containing tau/>
set -uo pipefail
ROOT="$(readlink -f "$1")/tau"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'kill "${SRV:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT
PY="$ROOT/$(sed -n 's/.*"interpreter": "\([^"]*\)".*/\1/p' "$ROOT/manifest.json")"
PORT=18798
"$PY" -I -B "$HERE/stub_openai.py" "$PORT" & SRV=$!
sleep 1
mkdir -p "$WORK/home/.tau"
echo "{\"default_model\": \"stub\", \"models\": {\"stub\": {\"backend\": \"openai\", \"model\": \"stub\", \"base_url\": \"http://127.0.0.1:$PORT/v1\", \"api_key\": \"x\"}}}" \
    > "$WORK/home/.tau/config.json"
clean() { env -i PATH=/usr/bin:/bin HOME="$WORK/home" TERM=xterm-256color TAU_PORTABLE_BASE="$WORK" "$@"; }

echo "== textual / rich importable?"
clean "$PY" -I -B -c 'import importlib.util as u; print({m: u.find_spec(m) is not None for m in ("textual", "rich", "prompt_toolkit")})'
echo "== modules loaded by the repl head's import path"
clean "$PY" -I -B -c 'import sys, tau_coding_agent.cli, tau_coding_agent.repl; print("textual loaded:", any(m.split(".")[0] == "textual" for m in sys.modules))'
echo "== tau -p"
clean "$ROOT/bin/tau" -p --no-session ping; echo "exit=$?"
echo "== tau --mode rpc: one prompt over stdin"
{ printf '%s\n' '{"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "ping"}}'; sleep 8; } \
      | clean timeout 20 "$ROOT/bin/tau" --mode rpc --no-session 2>&1 | cat > "$WORK/rpc.out"
echo "lines: $(wc -l < "$WORK/rpc.out")  agent_end: $(grep -c agent_end "$WORK/rpc.out")  error: $(grep -c "\"is_error\": *true\|\"error\":" "$WORK/rpc.out")"
echo "== tau --mode repl in a pty: type ping, wait, EOF"
{ sleep 4; printf 'ping\r'; sleep 6; printf '\004'; sleep 2; } \
    | clean timeout -s INT 20 script -qfec "stty cols 100 rows 30; '$ROOT/bin/tau' --mode repl --no-session" "$WORK/repl.out" >/dev/null 2>&1
echo "exit=$?  pong in output: $(grep -c pong "$WORK/repl.out")"
echo "== bare tau (the TUI) in the headless runtime"
clean "$ROOT/bin/tau" </dev/null 2>&1 | tail -3; echo "exit=$?"
