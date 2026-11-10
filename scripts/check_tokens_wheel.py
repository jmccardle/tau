"""The two wheels stay apart and in step (D26 §7.1, §7.2). Four checks:

1. **The dependency guard.** The main wheel gains nothing from the tokenizer wheel:
   `cargo tree -p tau-py -e normal` lists none of `tokenizers`, `tiktoken-rs`, `minijinja`,
   `minijinja-contrib`, `tau-tokens`.
2. **Lockstep.** Both wheels take their version from the Cargo workspace, and the `tokens` extra
   of `ffwf-tau` pins `ffwf-tau-tokens` to exactly that version, in PEP 440 form. The tokenizer
   wheel depends on nothing.
3. **No C.** Nothing the tokenizer wheel is built from needs a C toolchain or a system library
   (`cc`, `cmake`, Oniguruma, OpenSSL) or fetches anything (`hf-hub`, `ureq`, `reqwest`): that is
   what lets it build for every platform the main wheel builds for.
4. **No IO in `tau-tokens`.** Its source uses no `std::fs`, `std::net`, `std::process`,
   `std::io`, `std::thread`, `std::env` and no `async` (the scan of scripts/check_core_io.py).
   Its dependencies are not held to that: `tokenizers` can read files; this crate never asks.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import check_core_io

ROOT = Path(__file__).parents[1]
TOKENIZER_CRATES = {"tokenizers", "tiktoken-rs", "minijinja", "minijinja-contrib", "tau-tokens"}
NEEDS_C_OR_NETWORK = {
    *("cc", "cmake", "pkg-config", "bindgen", "onig", "onig_sys", "openssl-sys", "ring"),
    *("hf-hub", "ureq", "reqwest", "hyper", "native-tls", "rustls", "esaxx-rs-cpp"),
}
PRE_RELEASE = {"dev": ".dev", "alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc"}
WHEELS = {"ffwf-tau": "crates/tau-py", "ffwf-tau-tokens": "crates/tau-tokens-py"}


def toml(path: str) -> dict:
    return tomllib.loads((ROOT / path).read_text(encoding="utf-8"))


def closure(package: str, kinds: str) -> set[str]:
    """Names of every crate `package` is built from, over dependency edges of these kinds."""
    command = ["cargo", "tree", "-p", package, "-e", kinds, "--prefix", "none", "--locked"]
    out = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return {line.split(" ")[0] for line in out.splitlines() if line.strip()} - {package}


def pep440(version: str) -> str:
    """A Cargo version as maturin writes it into a wheel: `0.16.0-dev.0` is `0.16.0.dev0`."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-([a-z]+)\.(\d+))?", version)
    if match is None or (match[2] is not None and match[2] not in PRE_RELEASE):
        raise SystemExit(f"check_tokens_wheel: no PEP 440 form known for the version {version!r}")
    return match[1] + (PRE_RELEASE[match[2]] + match[3] if match[2] else "")


def lockstep() -> list[str]:
    problems = []
    for crate in WHEELS.values():
        if toml(f"{crate}/Cargo.toml")["package"]["version"] != {"workspace": True}:
            problems.append(f"{crate}/Cargo.toml: version must be `version.workspace = true`")
    version = pep440(toml("Cargo.toml")["workspace"]["package"]["version"])
    main, tokens = toml("pyproject.toml")["project"], toml("tokens/pyproject.toml")["project"]
    wanted = [f"ffwf-tau-tokens=={version}"]
    if main.get("optional-dependencies", {}).get("tokens") != wanted:
        problems.append(
            f"pyproject.toml: the `tokens` extra must be {wanted} (the workspace version), "
            f"not {main.get('optional-dependencies', {}).get('tokens')}"
        )
    if (main["name"], tokens["name"]) != tuple(WHEELS):
        problems.append(f"the wheels must be named {' and '.join(WHEELS)}")
    if "version" not in main["dynamic"] or "version" not in tokens["dynamic"]:
        problems.append("both pyproject.toml files must leave `version` to maturin (dynamic)")
    if tokens.get("dependencies"):
        problems.append(
            f"tokens/pyproject.toml: must depend on nothing, not {tokens['dependencies']}"
        )
    return problems


def source_io() -> list[str]:
    problems = []
    for file in sorted((ROOT / "crates" / "tau-tokens" / "src").rglob("*.rs")):
        for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//")[0]
            for label, pattern in check_core_io.SOURCE_PATTERNS.items():
                if re.search(pattern, code):
                    problems.append(f"{file.relative_to(ROOT)}:{n}: tau-tokens uses {label}")
    return problems


def main() -> int:
    problems = [
        f"tau-py (the main wheel) depends on {name}: tokenizers belong to ffwf-tau-tokens"
        for name in sorted(closure("tau-py", "normal") & TOKENIZER_CRATES)
    ]
    tokens = closure("tau-tokens-py", "normal,build")
    if not tokens >= TOKENIZER_CRATES:
        problems.append(f"tau-tokens-py no longer builds from {sorted(TOKENIZER_CRATES - tokens)}")
    problems += [
        f"tau-tokens-py is built from {name}: a C toolchain, a system library or a network"
        for name in sorted(tokens & NEEDS_C_OR_NETWORK)
    ]
    problems += lockstep() + source_io()
    for problem in problems:
        print(f"check_tokens_wheel: {problem}", file=sys.stderr)
    if not problems:
        version = pep440(toml("Cargo.toml")["workspace"]["package"]["version"])
        print(
            f"the wheels are apart and in step: ffwf-tau and ffwf-tau-tokens {version}; tau-py has "
            f"no tokenizer crate; tau-tokens-py is built from {len(tokens)} crates, none needing C "
            "or a network; tau-tokens does no IO"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
