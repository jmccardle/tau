# Quickstart

Install τ, write a config with one model, talk to it, and look at what you did.

The `tau` program is not finished. `tau` with no command prints its help, and the terminal UI and
`tau -p` (print one answer and exit) arrive in Phase 8. Today you chat with `examples/chat.py`,
and `tau` itself inspects, configures and serves sessions.

## Install

τ is the package `ffwf-tau`. It needs Python 3.12 or later and installs the `tau` command. Until
a release is on PyPI, build it from a checkout (Rust is needed for that):

```bash skip
python3.12 -m venv .venv
.venv/bin/pip install --group dev           # pip 25.1 or later
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release
source .venv/bin/activate                   # puts `tau` on your PATH
```

Everything τ keeps lives in `$TAU_HOME`, which is `~/.tau` unless you set it. The examples in this
guide run with a temporary `TAU_HOME` and the mock model, which needs no server and no key.

## A config with one model

`$TAU_HOME/config.json` names your models. A **group** holds what its models share (here, the
server) and a model's **qualified name** is the group, a colon and the model: `local:qwen`.

```json file=$TAU_HOME/config.json
{
  "default_model": "local:qwen",
  "models": {
    "local": {
      "base_url": "http://localhost:8100/v1",
      "models": { "qwen": { "model": "qwen3-27b" } }
    },
    "mock": {
      "provider": "mock",
      "models": { "echo": { "model": "echo" } }
    }
  }
}
```

`local` is any server that speaks the OpenAI chat API (llama.cpp, vLLM, a hosted service). For a
hosted one, add `"api_key_env": "NAME_OF_YOUR_KEY_VARIABLE"` to the group: the config names the
variable and never holds the key. `mock` answers by echoing you. [Configuration](configuration.md)
has the rest.

Check the file. It prints nothing when it is sound, and one line per problem otherwise, with the
line and column of a JSON mistake.

```bash
tau config check
tau config check --show local:qwen
```

The second command shows each resolved value and the file it came from.

## Chat

From a checkout, `examples/chat.py` is a line-by-line chat over the same library a terminal UI
would use. `--model` takes a qualified name or a bare one, and the config's `default_model` is
used without it.

```bash skip
python examples/chat.py                       # your default model
python examples/chat.py --model mock:echo     # no server needed
```

Type a message and the reply streams in. `/` alone lists the commands and Tab completes them.
Ctrl-D leaves.

## Look at what you did

The commands below work on sessions on disk. The script makes one the way `chat.py` does, with
the mock model, and names it `quickstart`; skip it if you have chatted already and use your own
session's id.

```python
import argparse
import asyncio
import os
from pathlib import Path

from tau import first_cursor, start_session
from tau.cli import extension_flags, session_flags


async def main() -> None:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    args = parser.parse_args(["--name", "quickstart"])
    started = await start_session(args, environ=os.environ, cwd=Path.cwd(), model="mock:echo")
    client = started.driver.client("quickstart")
    cursor = await first_cursor(started.session, client)
    await client.send(cursor, "hello from a script")
    await started.driver.rest(cursor)
    await client.close()
    await started.stop()


asyncio.run(main())
```

`tau sessions` lists the sessions of the current directory, newest first: id, age, and your first
message or the name you gave. `--all` adds the directory; `--json` prints one object per line.

```bash
tau sessions
```

```text expect
<*>   now  quickstart
```

```bash
tau sessions --json
```

`tau export` writes a session as Markdown, to standard output or to `-o FILE`. A session is named
by its id, any unique start of the id, its name, or a path.

```bash
tau export quickstart | head -n 12
```

`tau context` shows what a new session in this directory would tell the model: the built-in base
text, any `--append-system-prompt` you give, and the `AGENTS.md` and `CLAUDE.md` files around the
directory, each with its size in bytes. `--show` prints the whole text.

```bash
tau context
tau context --show --append-system-prompt "Answer in one line." | tail -n 3
```

`tau ext list` shows every extension source and why it is on or off. Extensions add commands,
tools and hooks to a session; [Writing an extension](extensions.md) shows how. The built-in
`base_tools` is what gives the model `read`, `write`, `edit`, `grep`, `find` and `bash`.

```bash
tau ext list
```

## Serve a session

`tau serve` runs one session and lets other programs drive it over a Unix socket: an editor
extension, a script, a second window. `-d` runs it in the background and `--json` prints where to
find it.

```bash
tau serve -d --json --session quickstart
tau serve --stop --session quickstart
```

The first command prints one JSON object: the `socket` to connect to, the server's `pid`, the
`session` and its working directory `cwd`. `--stop` ends the server. Connect from Python with
`RemoteClient` ([Writing a frontend](frontends.md#over-the-wire)), or from any language with
[the wire protocol](protocol.md).

## Where next

- [The model in ten minutes](model.md): what a session, a cursor and a step are.
- [Writing an extension](extensions.md) and [writing a frontend](frontends.md).
- [Configuration](configuration.md): layers, groups, project config and trust.
