#!/usr/bin/env bash
# Install the CI toolchain into a bare Linux x86_64 job: rustup (stable, with
# clippy and rustfmt) and uv, then a venv at .venv for the requested Python.
#
#   scripts/ci-toolchain.sh 3.12      # or 3.14t
#
# Every version is pinned here. The job image only needs curl (installed via
# apt if missing). Exports PATH additions through $GITHUB_PATH when run under
# Forgejo/GitHub Actions.
set -euo pipefail

PYTHON="${1:?usage: ci-toolchain.sh <python version, e.g. 3.12 or 3.14t>}"
UV_VERSION="0.13.5"

if ! command -v curl >/dev/null; then
    apt-get update -qq
    apt-get install -y --no-install-recommends curl ca-certificates build-essential
fi

curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal -c clippy,rustfmt
curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-installer.sh" | sh
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
if [ -n "${GITHUB_PATH:-}" ]; then
    echo "$HOME/.cargo/bin" >>"$GITHUB_PATH"
    echo "$HOME/.local/bin" >>"$GITHUB_PATH"
fi

uv python install "$PYTHON"
uv venv --seed --python "$PYTHON" .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python --version
