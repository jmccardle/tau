#!/usr/bin/env bash
# smoke_tui.sh — experiment: start the bundled TUI in a pseudo-terminal for a few seconds, clean env.
#
# Usage: smoke_tui.sh <unpacked tau dir> ; prints bytes rendered and the sync-output query seen.
set -euo pipefail
TAU_DIR="$(readlink -f "${1:?usage: smoke_tui.sh <unpacked tau dir>}")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/home/.tau"
cat > "$WORK/home/.tau/config.json" <<'JSON'
{"default_model": "stub", "models": {"stub": {"backend": "openai", "model": "stub", "base_url": "http://127.0.0.1:9/v1", "api_key": "x"}}}
JSON
cd "$WORK"
set +e
env -i PATH=/usr/bin:/bin HOME="$WORK/home" TERM=xterm-256color COLORTERM=truecolor \
    timeout -s INT 8 script -qfec "stty cols 120 rows 40; '$TAU_DIR/bin/tau'" "$WORK/typescript" </dev/null >/dev/null 2>&1
rc=$?
set -e
echo "exit=$rc (124/130 = killed by timeout, i.e. it was still running)"
echo "bytes rendered: $(wc -c < "$WORK/typescript")"
grep -c $'\e\[?2026\$p' "$WORK/typescript" | sed 's/^/DECRQM 2026 queries: /'
grep -c $'\e\[>[0-9]*u' "$WORK/typescript" | sed 's/^/kitty keyboard pushes: /'
grep -ao 'tau [0-9.]*\|τ' "$WORK/typescript" | sort | uniq -c | head -5
