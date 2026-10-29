#!/usr/bin/env bash
# Install the CI toolchain into a bare Linux x86_64 job: rustup (stable, with
# clippy and rustfmt) and uv, then a venv at .venv for the requested Python.
#
#   scripts/ci-toolchain.sh 3.12      # or 3.14t
#
# Every version is pinned here. The job image only needs curl (installed via
# apt if missing). Installs the Rust analysis tools (see versions above). Exports PATH additions through $GITHUB_PATH when run under
# Forgejo/GitHub Actions.
set -euo pipefail

PYTHON="${1:?usage: ci-toolchain.sh <python version, e.g. 3.12 or 3.14t>}"
UV_VERSION="0.13.5"
# Rust tools, installed from crates.io with --locked into ~/.cargo/bin.
# rust-code-analysis-cli is needed by check.sh (scripts/ratchet.py); the rest
# only by scripts/metrics.sh and scripts/coverage.sh, so they are installed
# when TAU_CI_METRICS=1 (the `report` CI job).
RCA_VERSION="0.0.25"
LLVM_COV_VERSION="0.9.1"
MACHETE_VERSION="0.9.2"
MODULES_VERSION="0.28.0"
DEPGRAPH_VERSION="1.6.0"

if ! command -v curl >/dev/null; then
    apt-get update -qq
    apt-get install -y --no-install-recommends curl ca-certificates build-essential
fi

curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal -c clippy,rustfmt \
    ${TAU_CI_METRICS:+-c llvm-tools-preview}
curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-installer.sh" | sh
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
if [ -n "${GITHUB_PATH:-}" ]; then
    echo "$HOME/.cargo/bin" >>"$GITHUB_PATH"
    echo "$HOME/.local/bin" >>"$GITHUB_PATH"
fi

cargo install --locked rust-code-analysis-cli --version "$RCA_VERSION"
if [ -n "${TAU_CI_METRICS:-}" ]; then
    if ! command -v dot >/dev/null; then
        apt-get install -y --no-install-recommends graphviz
    fi
    cargo install --locked cargo-llvm-cov --version "$LLVM_COV_VERSION"
    cargo install --locked cargo-machete --version "$MACHETE_VERSION"
    cargo install --locked cargo-modules --version "$MODULES_VERSION"
    cargo install --locked cargo-depgraph --version "$DEPGRAPH_VERSION"
fi

uv python install "$PYTHON"
uv venv --seed --python "$PYTHON" .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python --version
