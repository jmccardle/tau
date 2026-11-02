# Quickstart

Install τ, write a config with one model, talk to it, and look at what you did.

The `tau` program is not finished. `tau -p` sends one message and prints the answer, and `tau`
also inspects, configures and serves sessions. `tau` alone prints its help: the terminal UI
arrives in Phase 8, and until then you chat with `examples/chat.py`.

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

## Ask once

`tau -p` sends one message, waits for the model to finish, prints the answer on standard output
and exits. `-m` picks the model of a new session (the config's `default_model` is used without
it), `-n` names the session, and the session is kept, so `tau sessions` lists it.

```bash
tau -p -m mock:echo -n quickstart "hello from a script"
```

```text expect
mock: hello from a script
```

The words are the message. If standard input is not a terminal, what it holds follows them after a
blank line, so `git diff | tau -p "review this"` works. A program that runs `tau -p` with a pipe it
never closes passes `</dev/null`.

Only the answer is on standard output, so `answer=$(tau -p "...")` holds it. The exit status says
how the run ended: `0` the model finished, `1` a failure (a bad config, a provider error, a
command that failed), `2` a usage error, `3` it stopped before finishing (a step limit, a hook
that halted it, a message that was refused) and `130` interrupted. Anything else it has to say
goes to standard error, one line each, starting `tau: `. `--verbose` adds a report of the
start (the session, the model, the config files, the extensions, the parts of the system prompt)
and a line per message the run adds.

`--json` prints the session's events instead, one JSON object per line, the same objects
[the wire](protocol.md) sends, after a `Welcome` line that holds the session's id.
`--set step_limit.limit=5` changes an option for this run only ([Configuration](configuration.md)).
A question an extension asks has no one to answer it under `-p`, so `tau -p` declines it unless a
rule answers: `--answer permissions=yes`. [Frontends](frontends.md#questions) explain questions.

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

The commands below work on sessions on disk. The session `quickstart` is the one `tau -p` made
above; use your own session's id if you chatted instead.

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
