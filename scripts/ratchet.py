"""Rule 7 ratchet over crates/*/src (CLAUDE.md). Python is out of scope until Phase 4.

Checks:
  * function CC <= 15
  * no type over 25 methods (inherent impls; trait impls are reported, not gated),
    unless the type carries a one-line justification naming the single concept it is
    (`[justified]` in ratchet-allow.toml). The justification replaces the count for
    that type and is printed on every run, so it shows in review.
  * no file over 800 lines
  * banned-term count 0 (scripts/check_rules.py)
  * the I7 property tests still exist (cargo test runs them; check.sh runs that)

Known violations are listed with their current value in scripts/ratchet-allow.toml.
The list only shrinks: the ratchet fails when an allowed item gets worse, when
a new violation appears, and when an allowed item is fixed or improved but its
line is still there. `--tighten` rewrites the file to lower or remove lines
(never to add or raise one).

The allowlist file is itself guarded (its numeric sections; a justification may be added,
and the ratchet prints it): it is compared with its version at a base
revision and the ratchet fails if any value rose or any line was added (see
`base_revision`). There is no flag to skip that.

Measurement definitions: scripts/rustmetrics.py.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import check_rules
import rustmetrics

ROOT = rustmetrics.ROOT
ALLOW = Path(__file__).with_name("ratchet-allow.toml")
LIMITS = {"function_cc": 15, "type_methods": 25, "file_lines": 800}
SECTIONS = {
    "function_cc": "Function cyclomatic complexity (crate-relative file::Type::fn)",
    "type_methods": "Inherent methods per type (crate::Type)",
    "file_lines": "Lines per file",
}
JUSTIFIED_TITLE = (
    '# Types over the method limit that are one concept: crate::Type = "the one concept it is".\n'
    "# Exempt from the count and printed on every run. Adding one needs a reviewer's yes."
)
I7_TESTS = [
    ("crates/tau-core/tests/replay.rs", "reload_at_any_sync_equals_the_state_at_that_sync"),
    ("crates/tau-store/tests/store.rs", "a_torn_tail_opens_as_the_last_sync_before_it"),
]


def measure() -> dict[str, dict[str, int]]:
    """Current value of everything rule 7 limits, keyed for the allowlist."""
    m = rustmetrics.analyze()
    cc: dict[str, int] = {}
    for f in m["src"]["functions"]:
        key = f"{f['file']}::{f['owner'] + '::' if f['owner'] else ''}{f['name']}"
        cc[key] = max(cc.get(key, 0), f["cc"])  # same-named methods in one file: the worst
    methods = {
        f"{t['crate']}::{t['type']}": t["methods"]
        for t in m["src"]["types"]
        if t["impl"] == "inherent"
    }
    lines = {f["file"]: f["lines"] for f in m["files"] if f["kind"] == "src"}
    return {"function_cc": cc, "type_methods": methods, "file_lines": lines}


def load_allow() -> dict[str, dict[str, int]]:
    data = tomllib.loads(ALLOW.read_text())
    return {s: dict(data.get(s, {})) for s in SECTIONS}


def load_justified() -> dict[str, str]:
    """Type (crate::Type) -> the one-line reason it is exempt from the method count."""
    return dict(tomllib.loads(ALLOW.read_text()).get("justified", {}))


def check_justified(now: dict[str, dict[str, int]], justified: dict[str, str]) -> list[str]:
    """A justification must name a real type over the limit, and say something."""
    fails: list[str] = []
    limit = LIMITS["type_methods"]
    for key, reason in sorted(justified.items()):
        count = now["type_methods"].get(key)
        if not reason.strip():
            fails.append(f"EMPTY    justified: {key} has no reason")
        elif count is None:
            fails.append(f"GONE     justified: {key} no longer exists; remove it from {ALLOW.name}")
        elif count <= limit:
            fails.append(
                f"UNNEEDED justified: {key} has {count} methods (limit {limit}); "
                f"remove it from {ALLOW.name}"
            )
    return fails


def compare(
    now: dict[str, dict[str, int]],
    allow: dict[str, dict[str, int]],
    justified: dict[str, str],
) -> tuple[list[str], dict]:
    """Return (failures, tightened allowlist). A justified type is exempt from its count."""
    fails: list[str] = []
    tight: dict[str, dict[str, int]] = {s: {} for s in SECTIONS}
    for sec, limit in LIMITS.items():
        for key, val in sorted(now[sec].items()):
            allowed = allow[sec].get(key)
            if sec == "type_methods" and key in justified:
                if allowed is not None:
                    fails.append(f"FIXED    {sec}: {key} is justified; remove it from {ALLOW.name}")
                continue
            if val > limit and allowed is None:
                fails.append(f"NEW      {sec}: {key} = {val} (limit {limit})")
            elif allowed is not None and val > allowed:
                fails.append(f"WORSE    {sec}: {key} = {val} (allowed {allowed}, limit {limit})")
            elif allowed is not None and val <= limit:
                fails.append(
                    f"FIXED    {sec}: {key} = {val} is within {limit}; remove it from {ALLOW.name}"
                )
            elif allowed is not None:
                if val < allowed:
                    fails.append(
                        f"BETTER   {sec}: {key} = {val}; lower its {ALLOW.name} line from {allowed}"
                    )
                tight[sec][key] = val
        for key in sorted(set(allow[sec]) - set(now[sec])):
            fails.append(f"GONE     {sec}: {key} no longer exists; remove it from {ALLOW.name}")
    return fails, tight


def write_allow(tight: dict[str, dict[str, int]], justified: dict[str, str]) -> None:
    titles = {f"# {title}; limit {LIMITS[sec]}" for sec, title in SECTIONS.items()}
    titles |= set(JUSTIFIED_TITLE.splitlines())
    lines = ALLOW.read_text().split("\n[", 1)[0].splitlines()
    head = "\n".join(line for line in lines if line not in titles).rstrip() + "\n"
    out = [head]
    for sec, title in SECTIONS.items():
        out.append(f"\n# {title}; limit {LIMITS[sec]}\n[{sec}]")
        out += [f'"{k}" = {v}' for k, v in sorted(tight[sec].items())]
    out.append(f"\n{JUSTIFIED_TITLE}\n[justified]")
    out += [f'"{k}" = {json.dumps(v)}' for k, v in sorted(justified.items())]
    ALLOW.write_text("\n".join(out) + "\n")


def banned_terms() -> list[str]:
    return [
        f"{p.relative_to(ROOT)}:{n}: banned term {term!r}"
        for p in check_rules.files()
        for n, line in enumerate(p.read_text().splitlines(), 1)
        for term in check_rules.banned_terms(line)
    ]


def missing_i7() -> list[str]:
    return [
        f"I7 test {name} not found in {path}"
        for path, name in I7_TESTS
        if not re.search(rf"\bfn {name}\b", (ROOT / path).read_text())
    ]


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], check=False, cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"ratchet: git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def base_revision() -> str:
    """The revision the allowlist may only shrink from.

    The merge-base of HEAD and origin/master. On master itself that is HEAD,
    which would compare the file with itself, so the base is then HEAD~1 (the
    last commit; CI runs on pushes to master). Without origin/master (a clone
    that never fetched it), HEAD~1 as well. CI must fetch full history.
    TAU_RATCHET_BASE overrides all of this: CI sets it to the revision before
    the push, so a push of several commits is checked as a whole.
    """
    override = os.environ.get("TAU_RATCHET_BASE", "").strip("0")
    if override:
        return git("rev-parse", os.environ["TAU_RATCHET_BASE"])
    head = git("rev-parse", "HEAD")
    try:
        base = git("merge-base", "HEAD", "origin/master")
    except SystemExit:
        base = head
    return git("rev-parse", "HEAD~1") if base == head else base


def guard_allow(allow: dict[str, dict[str, int]], base: str) -> list[str]:
    """Lines added to, or raised in, the allowlist since `base`."""
    rel = ALLOW.relative_to(ROOT).as_posix()
    old = tomllib.loads(git("show", f"{base}:{rel}"))
    fails: list[str] = []
    for sec in SECTIONS:
        before = old.get(sec, {})
        for key, val in sorted(allow[sec].items()):
            if key not in before:
                fails.append(f"ADDED    {sec}: {key} = {val} is not in {rel} at {base[:12]}")
            elif val > before[key]:
                fails.append(f"RAISED   {sec}: {key} {before[key]} -> {val} since {base[:12]}")
    if fails:
        fails.append(
            f"{rel} only shrinks. Fix the code instead; a rule change goes through "
            "CLAUDE.md and John, not through this file."
        )
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tighten", action="store_true", help="lower or drop allowlist lines that improved"
    )
    args = ap.parse_args()
    now, allow, justified = measure(), load_allow(), load_justified()
    fails, tight = compare(now, allow, justified)
    fails = guard_allow(allow, base_revision()) + check_justified(now, justified) + fails
    for key, reason in sorted(justified.items()):
        print(f"ratchet: JUSTIFIED {key} ({now['type_methods'].get(key, '?')} methods): {reason}")
    if args.tighten:
        # Only lines that still violate and did not get worse are kept, at their new value.
        if any(f.startswith(("NEW", "WORSE", "ADDED", "RAISED")) for f in fails):
            print(
                "ratchet: refusing to tighten while there are NEW or WORSE violations",
                file=sys.stderr,
            )
        else:
            write_allow(tight, justified)
            print(f"ratchet: rewrote {ALLOW.relative_to(ROOT)}")
            return 0
    fails += [f"BANNED   {b}" for b in banned_terms()] + [f"I7       {m}" for m in missing_i7()]
    listed = sum(len(v) for v in allow.values())
    if fails:
        print(f"ratchet: {len(fails)} problem(s):", file=sys.stderr)
        for f in fails:
            print("  " + f, file=sys.stderr)
        return 1
    print(
        f"ratchet ok: {listed} allowed violation(s) remain "
        f"({', '.join(f'{len(v)} {s}' for s, v in allow.items())})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
