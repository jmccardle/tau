"""The real tokenizer files and the reference values the `tau_tokens` tests compare against.

    scripts/tokens_goldens.py fetch          download the pinned files into target/tokens-files/
    scripts/tokens_goldens.py fetch --pin    the same, and pin the sha256 of a file not yet pinned
    scripts/tokens_goldens.py present        say whether the pinned files are all there (no network)
    scripts/tokens_goldens.py generate       rewrite tests/fixtures/tokens/ from the references
    scripts/tokens_goldens.py generate --dump DIR   also write every rendered request under DIR
    scripts/tokens_goldens.py generate --retrain-tiny   also train tests/fixtures/tokens/tiny anew
    scripts/tokens_goldens.py sweep          tau_tokens against the references, every code point

`fetch` needs only the standard library and a network. Every file is pinned twice in
tests/fixtures/tokens/artifacts.json: its repository's commit, and its own sha256, which a
download must match. The files are not checked in (a tokenizer.json is 5 to 35 MB); the tests that
read them say so and are skipped, with this command in the reason, when they are absent.

`generate` is the only thing in this repository that imports `transformers`, `tokenizers`,
`tiktoken` and `jinja2`. They are the reference implementations: what Hugging Face's Python stack
and OpenAI's tokenizer print for the same input is what a server running them counts. Run it from
a scratch environment that has them (the project's own has none of them, on purpose):

    python -m venv /tmp/ref && /tmp/ref/bin/pip install transformers tokenizers tiktoken jinja2
    /tmp/ref/bin/python scripts/tokens_goldens.py generate

`sweep` needs the reference packages and the built `tau_tokens` wheel in one environment. It
encodes every Unicode code point, in a setting that puts it next to letters, spaces, digits and
line breaks, and a few thousand random mixtures, with each tokenizer in both implementations,
and prints what differs. It is the wide check behind the goldens' narrow one: run it when the
regex engine, `tokenizers` or a tiktoken table changes.

What `generate` writes is small: hashes and counts for the real files (goldens.json), whole rendered
strings for the templates written for the tests (templates.json), and the tiny tokenizers the
offline tests load (tiny/). The inputs (texts.json, conversations.json, templates/) are written by
hand and read here. The versions of the reference packages are recorded in goldens.json.
"""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "tokens"
ARTIFACTS = FIXTURES / "artifacts.json"
FILES = ROOT / "target" / "tokens-files"
HUB = "https://huggingface.co"
# The template is in one of these two; a repository may lack either. config.json has the model's
# own vocabulary size (the tokenizer catalogue's `served`).
SMALL = ["tokenizer_config.json", "chat_template.jinja", "config.json"]
AGENT = "tokens-goldens/1"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_dir(artifact: dict[str, Any]) -> Path:
    return FILES / artifact["repo"] / artifact["revision"]


def download(artifact: dict[str, Any], name: str) -> bytes | None:
    """The bytes of one file at the pinned commit, or None when the repository has no such file."""
    url = f"{HUB}/{artifact['repo']}/resolve/{artifact['revision']}/{name}"
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as reply:
            return reply.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:  # noqa: PLR2004
            return None
        raise SystemExit(f"fetch: {url}: HTTP {err.code} {err.reason}") from err


def fetch_one(artifact: dict[str, Any], name: str, pin: bool) -> str:
    """Make sure one file is on disk and is the pinned one. Returns what happened, in a word."""
    pinned: dict[str, str] = artifact.setdefault("files", {})
    path = artifact_dir(artifact) / name
    if path.exists() and name in pinned:
        if sha256(path.read_bytes()) != pinned[name]:
            raise SystemExit(f"fetch: {path} is not the pinned file (sha256 differs); delete it")
        return "present"
    if name not in pinned and not pin:
        raise SystemExit(f"fetch: {artifact['repo']} {name} is not pinned; run fetch --pin")
    data = download(artifact, name)
    if data is None:
        if name in pinned:
            raise SystemExit(f"fetch: {artifact['repo']} no longer has {name} at its pinned commit")
        return "absent"
    if name in pinned and sha256(data) != pinned[name]:
        raise SystemExit(f"fetch: {artifact['repo']} {name}: the download is not the pinned file")
    pinned[name] = sha256(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return "fetched"


def wanted(artifact: dict[str, Any], pin: bool) -> list[str]:
    names = SMALL + (["tokenizer.json"] if artifact["count"] else [])
    return names if pin else [n for n in names if n in artifact.get("files", {})]


def fetch(pin: bool) -> int:
    manifest = json.loads(ARTIFACTS.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        for name in wanted(artifact, pin):
            print(f"{artifact['repo']}@{artifact['revision'][:12]} {name}: ", end="", flush=True)
            print(fetch_one(artifact, name, pin))
    if pin:
        ARTIFACTS.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


def present() -> int:
    """Whether every pinned file is on disk and is the pinned one: no network. Exit status 0 when
    they all are, 1 (and what is missing) when not."""
    missing = []
    for artifact in json.loads(ARTIFACTS.read_text(encoding="utf-8"))["artifacts"]:
        for name, pinned in artifact["files"].items():
            path = artifact_dir(artifact) / name
            if not path.exists() or sha256(path.read_bytes()) != pinned:
                missing.append(f"{artifact['repo']} {name}")
    if missing:
        print(f"present: {len(missing)} pinned files are not in {FILES.relative_to(ROOT)}/")
        print("present: scripts/tokens_goldens.py fetch downloads them (about 170 MB)")
    return 1 if missing else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fetching = commands.add_parser("fetch")
    fetching.add_argument("--pin", action="store_true")
    commands.add_parser("present")
    generating = commands.add_parser("generate")
    generating.add_argument("--dump", type=Path)
    generating.add_argument("--retrain-tiny", action="store_true")
    sweeping = commands.add_parser("sweep")
    sweeping.add_argument("--seed", type=int, default=0)
    sweeping.add_argument("--rounds", type=int, default=5000)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return fetch(args.pin)
    if args.command == "present":
        return present()
    import tokens_reference  # noqa: PLC0415  (needs the reference packages; `fetch` must not)

    if args.command == "sweep":
        return tokens_reference.sweep(args.seed, args.rounds)
    return tokens_reference.generate(args.dump, args.retrain_tiny)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
