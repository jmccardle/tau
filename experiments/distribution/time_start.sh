#!/usr/bin/env bash
# time_start.sh — experiment: median wall time of N runs of a command, after one warm-up run.
#
# Usage: time_start.sh <N> <cmd...>
set -euo pipefail
N="$1"; shift
"$@" >/dev/null 2>&1 || true
for _ in $(seq "$N"); do
    s=$(date +%s%N); "$@" >/dev/null 2>&1 || true; e=$(date +%s%N)
    echo $(( (e - s) / 1000000 ))
done | sort -n | awk '{a[NR]=$1} END {printf "median %d ms (min %d, max %d, n=%d)\n", a[int((NR+1)/2)], a[1], a[NR], NR}'
