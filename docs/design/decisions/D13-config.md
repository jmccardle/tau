# D13: Configuration: one owned file, trusted projects, sessions name models

Status: accepted (2026-10-29). Amends D5.

## Context

τ 0.15 read `~/.tau/config.json` (or `$TAU_HOME/config.json`): a `models` map,
`default_model`, and frontend settings, with literal API keys in the example
file. Trusty so far has a minimal `ModelConfig` (model, base URL, the name of
the key variable) passed by hand, and every `Config` node holds that snapshot,
URL included.

Three needs:
- the same model under two accounts (a subscription key and a billing key);
- per-project settings, without a cloned repository being able to send your
  key to its own server;
- logs that hold no keys or machine-specific endpoints, so a session can be
  shared.

## Decision

**Files and layers.**
- The user config is `$TAU_HOME/config.json` (default `~/.tau`). Tau owns it:
  `tau` commands may rewrite it; hand edits stay valid.
- A project config is `.tau/config.json` in a trusted project directory.
- Layers, nearest wins: user, then project, then CLI flags. Inside and across
  files: nested objects merge by key; lists and scalars replace; `null` drops
  an inherited key.

**Groups and model names.**
- An object with a `models` object is a group; its other keys apply to every
  model inside it. Groups nest. A flat file with only `models` stays valid.
- A model's **qualified name** is its path of group names and its own name,
  joined by `:`: `free_tokens:gpt-6-sol`. Names may not contain `:` (the
  checker rejects one). Qualified names are unique by construction; bare names
  need not be.
- Selecting a model by a bare name (`--model gpt-6-sol`): one match is used;
  several matches prompt a live terminal to choose and fail a script with the
  list of qualified names.
- Merging across layers is by qualified name: a project entry at
  `free_tokens:gpt-6-sol` tunes the user's model of that name.

```json
{
  "models": {
    "free_tokens": {
      "api_key_env": "MY_SUBSCRIPTION_KEY",
      "models": { "gpt-6-sol": { "model": "gpt-6-sol" } }
    },
    "pay_by_usage": {
      "api_key_env": "MY_BILLING_KEY",
      "models": { "gpt-6-sol": { "model": "gpt-6-sol" } }
    }
  }
}
```

**A session names its model; it does not copy it.**
- A `Config` node holds the qualified name and nothing resolved from the file:
  no URL, no key variable, no sampling settings. The driver resolves the name
  against the current layers at each sample.
- Editing or renaming a model changes every session that names it. That is the
  user's call and the user's responsibility (chat history, prompt caches). A
  name that no longer resolves fails the sample: `model 'x' is not in your
  config`.
- An `Assistant` node keeps the metadata a response carries, such as the model
  the provider says answered, so an old session's replies say what produced
  them.

**Trusted projects.**
- `$TAU_HOME/trusted.json` lists trusted project directories. A `.tau/` that
  tau itself creates is trusted when created.
- Starting in a directory whose `.tau/` is not trusted, with a live terminal
  (stdin and stdout are TTYs), tau shows what the project config overrides and
  three checks: `.tau/` is owned by the user, its mode is `700`, and
  `.tau/.gitignore` exists (default content `*`). One approval trusts the
  directory, sets owner and mode, and writes the `.gitignore`.
- Without a live terminal, tau refuses to start and prints the same report and
  the command that trusts the directory (`tau trust <dir>`).
- Approval is consent to everything the project config sets, `base_url`
  included: the report shows it before the user agrees. What is approved is
  exactly what was shown: a config that changes during the prompt is refused,
  and the caller uses the approved text instead of reading the file again.
- A project config with errors cannot be approved; the report lists them.
- Content pinning decides. Symlinks and FIFOs (anything not a regular file) are
  refused for `.tau/`, its `config.json` and `.gitignore`, and `.env`; the open
  itself refuses them, so there is no earlier look to race. There is no owner
  check on `config.json` or `.env`: what is trusted is those exact bytes, and a
  file someone else wrote and the user approved is the user's decision. A
  `.tau/` *directory* owned by someone else is not a project (the search goes on
  past it): approval would have to `chmod` it, and it cannot.
- The report shows the project config itself, `api_key` values blanked, and
  lists the variable names a directory's `.env` sets, never their values.
  `.env` is pinned like the config: it can carry any variable, including the one
  `api_key_env` names, so a changed or added `.env` is untrusted again.
- `trusted.json` holds a hash of each approved config and of its `.env`, and a
  copy of the config with every `api_key` value blanked, for diffs; never a key. Trust operations refuse
  while `$TAU_HOME` or `trusted.json` is writable by others.

**Environment.**
- Shell environment variables are trusted. A project's `.env` is read only if
  its directory is trusted. CLI flags and the shell environment override
  `.env`.
- The `.env` reader is our own (`KEY=value`, quotes, comments), not
  `python-dotenv` when installed: one parse everywhere.
- `api_key_env` names a variable; the key is read at sample time and reaches no
  node and no log (tested). A literal `api_key` works; the checker warns.

**No schema version** before 1.0. A format change breaks old files, and the
examples are updated with it.

**`tau config check`** reports JSON errors with line and column, unknown keys
with their path and a suggestion, names containing `:`, unset variables, and
wrong types. `--show <model>` prints each resolved value and the layer and
group it came from. τ 0.15's `backend` key is rejected with "`backend` is now
`provider`".

The parser, trust check, and CLI are Python (`tau.config`, the `tau`
command). `tau-core` never reads config: `Config.data` stays opaque to it.

**Trust follows content.** `trusted.json` records each directory with a hash
of its `.tau/config.json` and of its `.env` (and a redacted config copy for diffs). A different hash is untrusted again: a live
terminal is prompted with the diff, a script fails. A `git pull` that changes
a committed project config cannot slip through.

**Finding the project.** The nearest `.tau/` from the working directory
upward, stopping at `$HOME`, as git finds `.git`. `tau trust DIR` where the
search finds none creates `DIR/.tau/` (mode `700`) and trusts it; `$HOME`,
`TAU_HOME`, anything inside it, and the directory whose `.tau/` is `TAU_HOME`
are refused. A symlinked or foreign `.tau/` at `DIR` itself is reported instead
of creating a project there; one further up is passed over, and `DIR/.tau/` is
created as usual. If approval then fails, the `.tau/` just created is removed.

## Alternatives

| option | why not |
|---|---|
| Model names unique file-wide | The same model under two keys needs two names the user invents and must remember. |
| `Config` node holds the resolved snapshot (old D5) | Endpoints and settings of one machine in every log; sessions do not travel. |
| Project files may tune but not set `base_url` or `api_key_env` | Trust with a report of the overrides covers it, and keeps project files whole. |
| Ignore an untrusted project config in scripts | Runs with settings the user did not choose and does not see (Fail Early). |
| A schema version in files and nodes | Machinery before 1.0, when breaking is free. |

## Consequences

- D5 holds: `config_at` is still the nearest `Config` on the walk. What a
  `Config` node holds changes: a model name, not a snapshot.
- Glossary: qualified name, group, user config, project config, trusted
  directory, `TAU_HOME`, `tau config check`, `tau trust`. `ModelConfig`
  becomes the resolved model, built by the driver at sample time.
- Not spiked. Tests: a key never reaches a node or the log; an untrusted
  project fails a script; a bare name with two matches fails a script and
  lists both; a renamed model fails the sample with its name.

## Amended (D23, 2026-11-03): the top-level keys

The top-level keys are `models`, `default_model`, `extensions`, `load`, `system_prompt` (D20)
and `tui`. `tui` is "the frontend settings" this ADR named: the terminal UI's own, an object whose
only key is `theme` (a string; an unknown key or a non-string is a `ConfigError`, like any other
mistake in the files). A project config may not set it: a `tui` in a
project layer is a `ConfigError` ("a person's own setting"), so a cloned repository cannot choose
the colours of the person who opens it, and nothing is accepted and ignored. The user's file and
the command-line layer merge as the others do. Read by `tau._config_keys.tui_settings`, held as
`ModelCatalog.tui`.

## Amended (D23 8C-3, 2026-11-03): what τ writes

`UserConfig` (D1) writes the user's file in three places: `extensions.<name>.<field>`
(`save_options`), `load.<key>` (`save_load`) and now `tui.theme` (`save_theme`), the theme the
person picks in the terminal UI. Same rules: the file's other content stays, the layers are read
again, the write is atomic, a project layer has no write. `--theme` stays one run only and writes
nothing.

## Amended (2026-11-03): what `configure(save=True)` writes

The server decides, so a client needs no disk access. `configure(cursor, extension, values,
save=True)` checks `values`, then writes to `extensions.<name>` the options in `values` and those
a `Config` node on the cursor's branch set, wherever they differ from what the user's file holds;
options equal to the file's stay untouched, and `values: {}` saves the branch's own choices.
Options only the user, project or command-line layers set are not copied (a project's setting must
not leak into every other project). A refused value writes neither the node nor the file. No wire
change.

## Amended (D24 8E-2, 2026-11-03): `context_window` and `price`

A model's settings gain two keys, read like `max_tokens` and `options`:

- **`context_window`**: a positive integer, the model's context size in tokens. Optional; from the
  config only (D24 ruling 9). `ModelConfig.context_window`.
- **`price`**: an object with `input`, `output`, `cache_read` and `cache_write`, USD per million
  tokens, all four required (a model that charges nothing to write the cache repeats its `input`
  price). `ModelConfig.price` is a `Price` of `Decimal`s. A `price` with a field missing, an
  unknown field, a number below 0 or a value that is not a number is a `ConfigError` at the
  model, which `tau config check` reports at `models.<name>`. A `price` object merges by field
  across layers and down through groups, as `options` does, so a project can change one field.

Neither is read by a provider or put in a node. There is no `tau config show`: `tau config check` validates both, and the config files hold them.

## Built (8E-6, 2026-11-07): session settings, and config that names what is missing

**A config that names something missing never crashes a start.** An extension may be only a file
that gets deleted, so τ cannot rely on it cleaning up its config. Loading turns each of these into
a warning (`ConfigProblem` severity `warning`; `option_problems`) and ignores it: a `load` key
no source has (`true` too: D19 said it stopped the start), `extensions.NAME` for an extension
that is not loaded or takes no options, an option field the extension does not have
(`make_layer_options` leaves it out; `/config` and `tau config set` still refuse one), (this is the general rule that "`tokenizer` set while
`tokens` is off" will fall under), and a tool name nothing provides in an option
(`Options.missing_tools`: `permissions` rules, `agents.tools`). A value the extension's option
type refuses is still an error. The start collects the warnings (`Started.warnings`) and `tau`,
`tau -p` and `tau serve` print them on stderr; `tau config check` prints them with its other lines.

**What τ writes is validated first.** `tau config set` and `/config` (with or without `--save`)
refuse an option that names a missing tool (`save_options(tools=)` and `Options.check_config`),
so τ never writes a file that warns.

**Cleaning.** `tau config check --clean` removes what the warnings are about, plus option fields
the extension does not have or its type refuses; `--purge EXTENSION` (repeatable) turns an
extension off and removes all its options (a source that no longer exists just loses its keys).
They write the layers `--layer user|project` names, the user file by default; a changed project
file is untrusted until `tau trust`. Modules: `tau._config_clean`.

**Off is not missing.** `option_problems(off=)` keeps the options of a source that exists but is
not loaded quietly, and `--clean` removes only names that no source provides; tool names are not
cleaned while a non-built-in source is off (its tools are unknown). A write is checked only for
the fields it sets (`tool_problems(tools, only)`), so an old warning never blocks a new setting.
Each option warning names the layer file (`ModelCatalog.extension_sources`). `--clean` drops the
lists and objects it empties and keeps the file's mode; it says when it is skipped because an
extension failed to load.

**Session settings** replace the "CLI layer" as one named layer, nearest of all, for one start and
never written (`tau._session_settings`). Shape: the `extensions` and `load` keys of a file. Inputs:
the `TAU_SETTINGS` environment variable (a JSON object, shell environment only, never a project's
`.env`) and `--set`, which is nearer. `--set load.KEY=true|false` switches a source (this was the
misleading error: `--set` split `load.retry` as extension `load`, field `retry` and answered that
`load` is read from files only; the `load` switch now reaches `inspect_extensions` because the
layer is built before it), and `--set EXTENSION.FIELD=VALUE` is as before, checked by the loaded
extension's option type. A bad variable or `--set` fails before a session exists; a `--set
load.KEY` no source has is an error (a typed flag is a typo), where the same in a file or the
variable is a warning. A server's loaded session takes neither (`--set` is refused; the variable
is not read for it). Reviewed paths: `tau -p` (`tests/cli/test_session_settings.py`), the screen
(`tests/serve/test_tui_pty.py`), `tau serve` (`tests/serve/test_serve_command.py`).
