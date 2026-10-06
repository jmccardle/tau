#!/usr/bin/env bash
# smoke_portable.sh — experiment: prove an unpacked bundle runs relocated, with no Python on PATH.
#
# Usage: smoke_portable.sh <tau-*.tar.gz from scripts/build_runtime.py --archive>
set -euo pipefail
TARBALL="$(readlink -f "${1:?usage: smoke_portable.sh <tau-*.tar.gz>}")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/some where/else"
tar -xzf "$TARBALL" -C "$WORK/some where/else"
TAU="$WORK/some where/else/tau/bin/tau"
PY="$WORK/some where/else/tau/$(sed -n 's/.*"interpreter": "\([^"]*\)".*/\1/p' "$WORK/some where/else/tau/manifest.json")"
HOMEDIR="$WORK/home"
mkdir -p "$HOMEDIR"
cd "$WORK"
clean() { env -i PATH=/usr/bin:/bin HOME="$HOMEDIR" TERM=xterm-256color "$@"; }

echo "== which python3 on the clean PATH (expected: system or none, never the bundle)"
clean sh -c 'command -v python3 || echo none'
echo "== tau --version"
clean "$TAU" --version
echo "== tau --help (head)"
clean "$TAU" --help | head -5
echo "== interpreter identity"
clean "$PY" -I -c \
    'import sys, sysconfig; print(sys.version.split()[0], "prefix-in-bundle:", sys.prefix.endswith("tau/python"), "gil-disabled-build:", bool(sysconfig.get_config_var("Py_GIL_DISABLED")))'
echo "== import every heavy dep; report whether the GIL got re-enabled"
clean "$PY" -I -W error::RuntimeWarning -c '
import sys
import tau_coding_agent.app, tau_llm, pydantic_core, jiter, PIL.Image, cryptography.hazmat.bindings._rust, textual, anthropic, google.genai
gil = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
print("imports ok; GIL enabled at runtime:", gil)'
echo "== tau -p against an unreachable model (startup + fail-early error path)"
CFG="$HOMEDIR/.tau"; [ -d "$WORK/some where/else/tau/.tau" ] && CFG="$WORK/some where/else/tau/.tau"
mkdir -p "$CFG"
cat > "$CFG/config.json" <<'JSON'
{"default_model": "stub", "models": {"stub": {"backend": "openai", "model": "stub", "base_url": "http://127.0.0.1:9/v1", "api_key": "x"}}}
JSON
set +e
clean timeout 60 "$TAU" -p "hello" > "$WORK/p.out" 2>&1
rc=$?
set -e
echo "exit=$rc"; tail -5 "$WORK/p.out"
