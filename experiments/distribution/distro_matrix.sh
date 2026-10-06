#!/usr/bin/env bash
# distro_matrix.sh — experiment: run an unpacked bundle in stock distro containers that have no Python.
#
# Usage: distro_matrix.sh <unpacked dir containing tau/> [image...]
set -uo pipefail
DIR="$(readlink -f "${1:?usage: distro_matrix.sh <dir> [image...]}")"; shift
IMAGES=("$@")
[ ${#IMAGES[@]} -gt 0 ] || IMAGES=(debian:10 rockylinux:8 ubuntu:20.04 ubuntu:18.04 fedora:42 archlinux:latest)
PROBE='command -v python3 >/dev/null && echo "has python3: $(python3 -V 2>&1)" || echo "no python3"; ldd --version | head -1; /b/tau/bin/tau --version && /b/tau/python/bin/python3 -I -c "import tau_coding_agent.app, PIL.Image, cryptography.hazmat.bindings._rust, google.genai, anthropic; print(\"heavy imports ok\")"'
for img in "${IMAGES[@]}"; do
    echo "== $img"
    docker run --rm -v "$DIR:/b:ro" "$img" sh -c "$PROBE" 2>&1 | tail -4
done
