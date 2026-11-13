# Configuration

τ reads JSON config files, in layers, and keeps a few other things in `$TAU_HOME`. This page
covers where they are, what goes in them, and how a project's config is trusted. For a first
config, see [the quickstart](quickstart.md).

## Where things live

`$TAU_HOME` is `~/.tau` unless the variable says otherwise (it must be an absolute path, or start
with `~`). τ owns it:

| path | what |
|---|---|
| `config.json` | your config; `tau config set` and `tau ext enable` write it |
| `trusted.json` | the projects you have approved, with a hash of each one's files |
| `sessions/` | one directory per session |
| `extensions/` | your extensions, one `.py` file or package directory each, called drop-ins ([Writing an extension](extensions.md)) |
| `serve/` | the sockets and records of `tau serve` |
| `AGENTS.md` or `CLAUDE.md` | instructions for every session (see [Context files](#context-files-and-the-system-prompt)) |

Keep `$TAU_HOME` private: τ refuses to approve a project while `$TAU_HOME` or `trusted.json` is
writable by other users, and refuses an `extensions/` directory they can write. `tau config set`
and `tau ext enable` rewrite `config.json`: your keys and values stay, and the layout is τ's, so
hand edits stay valid but your formatting does not survive.

## Layers

There are three layers, and the nearest wins:

1. **user**: `$TAU_HOME/config.json`;
2. **project**: `.tau/config.json` in a trusted project (below);
3. **session**: for one run, never written: `--system-prompt`, `--set`, and the `TAU_SETTINGS`
   environment variable (below).

Layers merge as JSON: objects merge by key, and lists and scalars replace. A `null` removes a key
inherited from a lower layer. So a project can change one setting of one of your models and leave
the rest.

The top-level keys:

| key | what |
|---|---|
| `models` | your models, in groups (below) |
| `default_model` | the qualified name used when a program is not told which |
| `extensions` | per-extension options: `{"step_limit": {"limit": 30}}` |
| `load` | which extension sources are on: `{"shouter": false}` |
| `system_prompt` | the base text of the system prompt |
| `context_files` | `false` leaves the context files out of the system prompt (default `true`) |
| `tui` | the terminal UI's own settings: `{"theme": "latte"}` (below) |

An unknown key is an error that names the closest key you might have meant.

## Models and groups

A model has these keys: `model` (the name the API knows), `provider`, `base_url`, `api_key_env`,
`max_tokens` and `options`. An object with a `models` object inside it is a **group**: its other
keys apply to every model in it, and groups nest. A model's **qualified name** is the path of
group names and its own name joined by colons, such as `work:fast`; names may not hold a colon.

```json file=$TAU_HOME/config.json
{
  "default_model": "work:fast",
  "models": {
    "work": {
      "base_url": "https://api.example.com/v1",
      "api_key_env": "WORK_API_KEY",
      "models": {
        "fast": { "model": "example-fast" },
        "big": { "model": "example-large", "max_tokens": 8000 }
      }
    },
    "personal": {
      "base_url": "https://api.example.com/v1",
      "api_key_env": "PERSONAL_API_KEY",
      "models": { "fast": { "model": "example-fast" } }
    },
    "claude": {
      "provider": "anthropic",
      "api_key_env": "ANTHROPIC_API_KEY",
      "max_tokens": 16000,
      "models": { "sonnet": { "model": "claude-sonnet-5-5" } }
    }
  },
  "extensions": { "step_limit": { "limit": 30 } }
}
```

`work:fast` and `personal:fast` are the same model under two accounts, which is what groups are
for. A program that is asked for `fast` has two matches: a terminal asks which; a script fails
with the list.

- **`provider`** is `openai` (the default: any OpenAI-compatible chat API, such as llama.cpp,
  vLLM or OpenAI), `anthropic`, `gemini`, or `mock` (echoes, for trying things). `base_url` is
  needed for `openai`.
- **`options`** are the provider's own settings, such as `temperature`, `thinking` or an
  `extra_body` to pass through. The provider names the ones it accepts, and `tau config check`
  rejects the rest. They are listed in [the providers reference](../reference/providers.md).
- **The key** is never in the config. `api_key_env` names an environment variable that is read when
  a sample is made, and it reaches no node and no log. A literal `"api_key"` works but warns. Set
  one or the other, not both.

A session records the *name* of its model, not the settings. Editing a model in your config
therefore changes every session that names it, and a name that no longer resolves fails that
session's next sample. `/model NAME` in a session switches that branch to another configured model.

### `tau config check`

```bash
tau config check
tau config check --show work:big
```

`check` reads every config file and prints one line per problem as `file:line:column: severity:
message`, or a dotted path to the setting when there is no line. A JSON mistake gets a line and
column; an unknown key gets a suggestion. Errors make the exit status 1; warnings (an unset key
variable, a literal key) do not. Options of an extension that is switched off are kept without
a warning. It also loads your extensions and checks their options.

Configuration that names something this start does not have is a warning, never an error, and is
ignored: a `load` key no extension has, options of an extension that is not loaded (deleted, or
turned off), options of an extension that takes none, an option the extension does not have, and a tool name nothing provides (a
`permissions` rule, an `agents` `tools` name). An extension may be nothing but a file that gets
deleted, so τ cannot count on it cleaning up its own settings. A *value* the extension's option
type refuses is still an error. `/config` and `tau config set` refuse an unknown option outright.
`--show NAME` prints each resolved value of one model with the file and group it came from. A key's
value is never printed.

```bash skip
tau config check --clean
tau config check --purge agents
tau config check --clean --layer project
```

`--clean` removes what the warnings are about (only for names that no extension source provides
from where you run it; the options of an extension that is merely switched off stay, and so do tool
names while such an extension might provide them), and any option the extension's type refuses, from
your config file, and says what it took out. `--purge EXTENSION` (repeatable) turns the extension
off (`load.EXTENSION = false`) and removes all of its options, so it starts from its defaults when
you enable it again; for an extension that no longer exists it just removes its keys. They write
your user file only; `--layer project` (repeatable, with `--layer user`) writes the trusted
project's `.tau/config.json` instead, and then the project is untrusted until you run `tau trust`
again, because its content changed.

### `tau config set`

```bash
tau config set step_limit.limit=40
```

`tau config set EXTENSION.FIELD=VALUE` writes an extension's option into your user config. Every
value is checked by the extension's own option type first, and nothing is written if any is wrong.
It never writes a project's file. `tau ext enable KEY` and `tau ext disable KEY` write the `load`
key the same way.

### Session settings

```bash skip
tau -p --set step_limit.limit=5 "tidy the imports"
tau -p --set load.retry=false "no retries this time"
TAU_SETTINGS='{"load": {"retry": false}, "extensions": {"step_limit": {"limit": 5}}}' tau -p "go"
```

The session layer is the nearest one: it applies to one run, whichever program starts it (`tau`,
`tau -p`, `tau serve`), and is written nowhere. It has the shape of the `extensions` and `load` keys
of a config file and merges with the files by the same rules (objects by key, lists and scalars
replace, `null` drops). Nothing else is a session setting: models belong in the files, and
`--system-prompt` is its own flag.

- `--set EXTENSION.FIELD=VALUE` sets an extension's option, read the same way as `tau config set`
  and checked by the extension's option type. `--set load.KEY=true|false` switches an extension
  source on or off for the run. Both may be repeated.
- `TAU_SETTINGS` is a JSON object with `load` and `extensions` keys, for scripts. `--set` is
  nearer than it, key by key. It is read from the shell's environment only, never from a
  project's `.env`.

A mistake in a `--set` (an extension that has no options or is not loaded, a field it lacks, a value
its type refuses, a `load` key no source has) or an invalid `TAU_SETTINGS` stops the start before a
session is made. `TAU_SETTINGS` naming something that is not there is only a warning, as in a file.
For a session a `tau serve` already holds, `--set` is an error and `TAU_SETTINGS` is not read: the
server's options are loaded. `--set` replaces 0.15's `--max-turns` and `--ext-config`.

## Projects and trust

A **project** is the nearest directory with a `.tau/`, starting at the working directory itself and
going up, as git finds `.git`. The search stops at your home directory, which is never a project,
and a directory whose `.tau` is `$TAU_HOME` is not one either. Started outside your home
directory, it runs up to `/`. Its `.tau/config.json` is the project layer.

A project config is code you did not write. It can set `base_url`, which decides where your key is
sent, and `.tau/extensions/` runs Python. So τ does not use a project until you approve it:

- Run `tau trust` in the project (or `tau trust DIR`). It prints the project's config (with key
  values blanked), its extension files with their sizes, and the names, never the values, that its
  `.env` sets; then it trusts the project.
- Approval is of the *exact bytes* of `.tau/config.json`, of `.env`, of every file under
  `.tau/extensions/`, of `.tau/SYSTEM.md`, and of the prompt templates and skills
  (`.tau/prompts/*.md`, `.tau/skills/*/SKILL.md`, [What τ does for you](built-ins.md)). If any changes, the project is untrusted again.
  A `git pull` cannot slip a new `base_url` past you.
- A project whose config has errors cannot be approved: the report lists them.
- `tau trust` in a directory with no `.tau/` creates and trusts one.

An untrusted project stops `tau` from starting a session. In a terminal, τ shows the same report
and asks. With no terminal (a script, `tau serve -d`), it refuses and names the command to run.
`tau config check` and `tau ext list` mark an untrusted project's files and still show what
they say.

```bash
mkdir -p project/.tau
echo '{"models": {"work": {"models": {"fast": {"options": {"temperature": 0}}}}}}' \
    > project/.tau/config.json
cd project
tau config check || echo "exit $?"
tau trust
tau config check --show work:fast
```

The first `check` fails with `project is not trusted; run tau trust`. After `tau trust`, `--show`
reports `temperature` as coming from the project's file.

### `.env`

A trusted project's `.env` (`KEY=value` lines, with quotes and comments) is read, so that the
variables an `api_key_env` names can come from it. The shell's own environment wins over it. A
malformed line stops the start and names the line, never the value.

## Context files and the system prompt

When a session is created, τ writes its system prompt as the first message (the `System` node),
and never again: **editing `AGENTS.md` changes no session that already exists.** It is built from
these parts, joined by blank lines:

1. the **base text**: the nearest layer's `system_prompt`, or the built-in text. `"system_prompt":
   ""` is no base text;
2. each `--append-system-prompt TEXT`, in the order given;
3. the **context files**, in a `<project_context>` block, each marked with its path: first an
   `AGENTS.md` (or, if there is none, `CLAUDE.md`) from `$TAU_HOME`, then one from each directory
   from `/` down to the working directory (the nearest comes last, so it reads as the most
   specific), then the trusted project's `.tau/SYSTEM.md`.

`--system-prompt TEXT` replaces the base text, and `-nc` (`--no-context-files`) leaves the context
files out. So does `"context_files": false` in a config file, for a program with no command line,
such as a `tau serve` started from a generated `config.json`; `-nc` and the key together are fine,
and a value that is not `true` or `false` is a config error. To use a file, let the shell read it: `--system-prompt "$(cat my.md)"`. The flags belong
to every program that starts a session: `tau serve`, `tau context`, `examples/chat.py` and your own.

`AGENTS.md` and `CLAUDE.md` need no trust: they only ask the model for things, and the model can
read them with its tools in any case. A context file that exists but cannot be read (permissions,
not UTF-8, a broken link) stops the start, and the message tells you about `-nc` and `context_files`.

`tau context` shows what a new session in this directory would be told, without making one: one
line for each part, with its size in bytes and where it came from (`base`, `config`, `flag`, or a
file's path). `--show` prints the text.

```bash
echo "Prefer small functions." > AGENTS.md
tau context
tau context -nc
tau context --show --system-prompt "You are brief." --append-system-prompt "Use tabs." | head -n 8
```

The prompt flags apply to a *new* session. With `-c`, `-r`, `--session` or `--fork`,
`--system-prompt` and `--append-system-prompt` are an error, since the session already has its
prompt (`-nc` is accepted). A program that passes its own `system=` is not told off for the
config key, which it does not use; it is told off for the flags. A program that has its own prompt passes it to
`start_session(system=...)` instead ([Writing a frontend](frontends.md)).

## The terminal UI's settings

The `tui` key holds the terminal UI's own settings; `theme` is the only one yet. It belongs to your file
(`$TAU_HOME/config.json`): a `tui` key in a project's `.tau/config.json` is an error, because a
project cannot choose your colours.

```json
{ "tui": { "theme": "latte" } }
```

The built-in themes are `mocha` (the default), `latte`, `gruvbox` and `ansi`, and Textual's own.
A theme of your own is a file `$TAU_HOME/themes/NAME.json` that `extends` one of those and
overrides parts of its `palette` (τ's colours) or its `textual` tokens; a file that does not read
is shown as a toast and skipped, and the rest still load. `--theme NAME` picks one for a single
run and writes nothing; picking a theme in the app (Ctrl+P, Theme) writes `tui.theme` to your file. A name that is no theme stops `tau` before it opens, and says which there are. An unknown
key under `tui` is an error like any other.

## Per-run flags

These are not config, and they last for one run:

| flag | what |
|---|---|
| `--session-dir DIR` | keep and find sessions in `DIR` instead of `$TAU_HOME/sessions` |
| `-e PATH\|KEY` | load this extension, even if it is off |
| `--set EXTENSION.FIELD=VALUE` | an extension's option for this run (above) |
| `-m NAME`, `--model NAME` | the model of a new session (`tau`, `tau -p`) |
| `--theme NAME` | the terminal UI's theme for this run (`tau`; not with `-p`) |
| `--no-extensions` | load no package or drop-in (the built-ins, `agents`, `attachments`, `base_tools`, `compaction`, `permissions`, `prune`, `skills`, `step_limit`, `tokens` and `usage`, and the `retry` wrapper, still follow `load`) |
| `-c`, `-r`, `--session REF`, `--fork REF`, `--no-session`, `-n NAME` | which session, and its name ([Quickstart](quickstart.md)) |

## When it fails

`tau` prints one line, `tau: message`, and exits 1; never a traceback. The usual causes and the
fix:

- *model 'x' is not in your config*: the name is misspelt (the message lists close ones) or
  the config file has an error; run `tau config check`.
- *project is not trusted*: run `tau trust DIR`.
- *warnings before the first message* (`tau: config: warning: ...`): your config names something
  this start lacks, such as a deleted extension's options. The start went on and ignored them;
  `tau config check --clean` removes them.
- *extension "x" failed to load; tau did not start*: the message names the source and the way
  past it, `tau ext disable x`, or `--no-extensions`.
- *session ... is in use by another process*: another `tau` or `tau serve` holds it. Work on a
  copy with `--fork`, or connect to the server ([Writing a frontend](frontends.md#over-the-wire)).
