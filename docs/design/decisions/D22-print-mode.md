# D22: `tau -p`: the one-shot client

Status: proposed 2026-11-02. Phase 8, stage 8B. Depends on D16 (clients, questions), D18 §5 (the
shared start), D21 (the wire's encoding; serve records). Answers `docs/from-0.15.md` §2.6
(headless approval) and the `-p`, `--mode json`, `--version` and `--verbose` rows of §2.8.

## Context

`tau -p "fix the test"` sends one message, waits for the agent to finish, prints the answer and
exits. It is the program scripts, cron jobs and editors call. It is also the simplest frontend:
one `Client`, one cursor, no person to ask.

**What exists (checked 2026-11-02).**
- `start_session(args, ...)` (D18 §5) resolves the session flags, changes into the session's
  directory, loads config and extensions, and starts the `Driver`. It takes `model=` (the
  program's `--model`) and `prompts=` (trust and model questions on a terminal). `-m` is not in
  `session_flags()`; each program adds it.
- `first_cursor(session, client)` picks the cursor: it needs the `Session`, so it works only on a
  local start.
- `Client` (D16 §5): `send` returns the item id; `CursorView` says `busy`, `auto`, the open
  `questions`, the pending `items`, the last `ending` and a `hook_error`. A step that ends `Done`
  with nothing left to do rests **with auto-step still on**; `StepLimit`, a halt, a failure, an
  abort or a step that did not move turns it off (model.md §6). So a cursor at rest with auto-step
  on means "the model finished".
- `OpenQuestion` is `(id, to, text, schema)`. **It does not say which extension asked**, though
  every handle knows its extension.
- The wire encodes every event as its fields plus `kind` (`tau._protocol.encode_event`;
  `$defs/Event` in `docs/protocol.schema.json`). `tau serve --json` prints one JSON object per
  line and a failure as `{"error": {"type", "message"}}` (D21 §9).
- `tau` with no subcommand prints help and exits 2. `tau.__version__` exists; no flag shows it.

**τ 0.15** (`git show v0.15.0:tau-coding-agent/src/tau_coding_agent/headless.py`, `cli.py`):
- The prompt was the positional words joined with newlines; stdin was never read. A message
  that was all `/command` ran the command.
- Text mode streamed the assistant text of every turn to stdout, concatenated with no separator.
  Retries, notices and load failures went to stderr with a `[τ] ` prefix.
- `--max-turns N` stopped silently. Exit 0 on success and on SIGINT, 1 on a provider or hook
  error, 2 on usage and config errors.
- `--mode json` wrote 0.15's bus events (`message_update` with deltas, `turn_end`, ...), a third
  shape beside the RPC wire's.
- `--ui-defaults METHOD=ANSWER` matched dialogs by method only. By 0.15.0 its only valid value
  was `form=defaults`; the help text's own example (`confirm=yes`) was refused. A dialog with no
  rule raised. STEP-DEBUGGER.md planned `--answer` and exit 3; neither was built.
- `-p` never attached to a daemon (`--connect` with `-p` was refused). A held session was an
  error. `--verbose` printed the parsed arguments; `-v/--version` printed `tau 0.15.0`.

## Decision

### 1. The command line

`tau -p [flags] [MESSAGE...]`. `-p/--print` is a switch, not a flag that takes the message.

- **Which parser.** The first argument decides: a subcommand's name (`sessions`, `serve`, ...)
  runs that subcommand; anything else is the launch command line (`tau`, or `tau -p`). So
  `tau -p sessions` sends the word "sessions".
- **The message** is the positional words joined with single spaces, then, if stdin is not a
  terminal, a blank line and everything read from stdin: `git diff | tau -p "review this"`. A
  program that runs `tau -p` with an open pipe it never closes must pass `</dev/null`, or `-p`
  waits for it. Nothing in either place is a usage error (exit 2), checked before the start, so
  it creates no session.
- **Flags** are D18's session flags, D19's `-e` and `--no-extensions`, D20's prompt flags, and
  `-m/--model`, `--json`, `--answer RULE` (§5), `--verbose` (§7). `-r` with `-p` is a usage
  error (D18).
- **No prompts.** `-p` passes no `prompts` to the start: an untrusted project fails with "run
  `tau trust`", and an ambiguous model fails listing the choices. A one-shot program asks nobody
  on its stdout.

### 2. The run

1. Open the session (§8: start it, or attach to the server that holds it). The client id is
   `<user>@<host>:print`.
2. Take the first cursor (D18 §5), and `watch` only it.
3. Send the message: `send(cursor, text)`. A message that starts with `/` is a command line
   instead: `parse_line`; an `UnknownCommand` is sent as text with a warning on stderr (as every
   built-in frontend does); missing required arguments are a usage error naming them; otherwise
   `run` it, in a task, so its questions are answered (§5) while it runs.
4. **Wait for rest:** the first `CursorView` of the cursor, after the call returned, that is not
   busy. A `send` wakes the cursor, so the view that call published is busy (D21 §3's order holds
   locally too). Meanwhile answer questions (§5).
5. Print (§3, §4), close the client, stop, and exit (§6). SIGINT aborts the cursor, waits for it
   to rest, stops and exits 130; a second SIGINT exits 130 at once.

`-p` leaves the session as any frontend does: a new session it used is kept and listed.
`--no-session` is the throwaway run.

### 3. Text output

- **stdout is the answer**: the text parts of the **last** `Assistant` node the run added to the
  cursor's walk, printed when the cursor rests, ending with one newline. Earlier replies ("Let me
  read the file") are progress, not the answer; `--verbose` shows them on stderr. A run that
  ends without a final reply prints nothing on stdout.
- It prints from the node (`Appended`), not from `Live`. `Live` is a preview a full buffer may
  drop (D16 §5); a pipe must get exactly what the log holds. Streaming text is `--json`'s and the
  TUI's (Open 1).
- **stderr** gets, one line each, prefixed `tau: `: every `Notice`; a `hook_error`; each
  question declined or answered by a rule (§5); why the run stopped when it did not finish (§6);
  and the failure that ended it.

### 4. JSON output: `--json`

One JSON object per line on stdout, nothing else there, in the wire's own encoding (D21 §4).

1. **First, a `Welcome`**, the type `hello` answers, with `"kind": "Welcome"`: `protocol`,
   `version`, `session` (the id; `null` with `--no-session`), `cwd`, `pid`. A script keeps the id
   for `--session`.
2. **Then every event the client hears**, each exactly as the wire's `event` notification
   params (`$defs/Event`): the snapshot, then `Appended`, every cursor's `CursorView`, `Labels`,
   `Notice`, and the watched cursor's `Live`. Nothing is filtered or summarised: a consumer that
   wants only the reply reads the last `Appended` `Assistant` node; one that wants streaming reads
   `Live`.
3. **On failure, a last line** `{"error": {"type", "message"}}`, as `tau serve --json` prints.
   A successful run just ends; its last `CursorView` of the cursor carries the `ending`, and the
   exit status says the rest (§6).

So `-p --json` is the event half of a wire connection, written to a file: a client generated
from `docs/protocol.schema.json` already reads it. There is no fourth format.

`--verbose` with `--json` still writes to stderr; stdout stays one object per line.

### 5. Questions: `--answer`

Ruling 1 (fix plan): a question never declines for want of someone to answer. Under `-p`
someone does answer: the `-p` client, by rule or by declining.

- **Which questions.** Every open question `-p` may answer (`to` is `None` or this client), on
  any cursor, while the run lasts. A sub-agent's permission question is on the sub-agent's
  cursor, and the run waits on it. (Attached to a server, see Open 3.)
- **A rule** is `--answer EXTENSION[:TEXT]=VALUE`, repeatable; the first that matches wins.
  `EXTENSION` is the asking extension's name or `*`. `TEXT` is a glob over the question's text
  (default `*`). `VALUE` is read by the question's schema with `commandline.value`, as a typed
  answer is: one of a `Literal`'s values (permissions asks `yes | always | no`, D24 §7), `yes`
  for a boolean, `3`, `a,b` for a list.
  ```
  tau -p --answer permissions=yes "tidy the imports"
  tau -p --answer 'permissions:bash: rm *=no' --answer 'permissions=yes' "clean up"
  ```
- **No rule matches: decline** (`answer(id, None)`), and say so on stderr: `tau: declined
  question 4 from permissions: "bash: rm -rf build"`. Declining is safe by D16 §3: the asker
  does as little as possible.
- **A rule whose value the schema refuses** is the person's mistake, not the extension's: `-p`
  aborts the cursor and exits 1, naming the rule, the question and the schema's complaint. It
  does not decline in its place.
- Standing policy ("always allow `read`") belongs in the asking extension's options (`tau config
  set permissions.allow=...`), not in a `-p` config key. `--answer` is per run.
- **This needs `OpenQuestion.extension`** (gap 1 below).

### 6. When it stops, and the exit status

| status | when |
|---|---|
| 0 | the cursor rested with auto-step on, ending `Done`, the message delivered: the model finished. For a command line, the command returned and any step it caused ended `Done`. |
| 1 | a failure: ending `Failed` (a sample or a hook), `CommandFailed`, a start error (`ConfigError`, `TrustError`, `Leased`, ...), a refused `--answer` value, the driver failed or the server went away |
| 2 | usage: bad flags, nothing to send, a command line missing arguments, `-r` |
| 3 | stopped before finishing: the cursor rested with auto-step off (`StepLimit`, a hook turned it off, a step did not move) or ending `Halted`, or the message is still pending (a `pre_input` hook refused it) |
| 130 | interrupted |

Exit 3 is new. 0.15 stopped silently at `--max-turns` and exited 0, so a script could not tell a
cut-off answer from a whole one. Status 3 always comes with a stderr line saying which.

`--max-turns N` is `StepLimit`'s option. For one run that is Open 4's `--set
step_limit.limit=N`.

### 7. `--version` and `--verbose`

- **`-v/--version`** prints `tau 0.16.0 (protocol 1)` and exits 0: the τ version and the wire
  `PROTOCOL` a client must speak. It is a flag of the launch command line only.
- **`--verbose`** writes to stderr, never stdout, for `tau -p` and `tau serve` (and `tau`, D23):
  - **the start report**, before the first message: the session (id and directory, or "in
    memory"; new, opened, forked or attached), its working directory, the model, the config
    files read, each extension loaded with its source key and origin, and for a new session the
    system prompt's parts with their sizes (`tau context`'s lines);
  - **under `-p`, the run as it goes**: one line per node the run adds to the cursor's walk
    (`node 5 Assistant: "Let me read..." + 2 calls: read, bash`; `node 6 Results: read ok, bash
    error`), each step's ending, and each question as it opens.
  
  It is a report of what happened, not a debug log: no arguments dump, no timings
  (`tau.trace` is the profiler).

### 8. Opening the session, and attaching to `tau serve`

`-p` and the TUI open a session the same way, in one library function, **`open_client(args, *,
client, environ, cwd, ...) -> Opened`**:

1. Resolve the session flags (D18 §5 step 0; writes nothing).
2. **If the choice is an existing session and a live `tau serve` holds it** (its serve record
   answers `hello`, D21 §9), connect a `RemoteClient` to it instead of starting: **attach**.
   `-e` and `--no-extensions` are then a `ConfigError` (the server's extensions are loaded; these
   would be ignored); `-m` and the prompt flags are refused already for an existing session
   (D18, D20); `-n` renames, which needs no lease. A session held by a process that is not a
   server is still `Leased`, suggesting `--fork`.
3. Otherwise `start_session` and `driver.client(client)`.
4. **Read the snapshot** from the client's stream (every event up to the first `Labels`) and
   pick the first cursor from it, reopening a closed one through the client. This works the same
   on both kinds of client, which `first_cursor(session, client)` cannot (gap 2).

`Opened` holds the `client`, the `cursor`, the `snapshot` events (so the caller loses none),
the `welcome` (a remote client's, or one built from the start for a local client) and `close()`
(close the client; for a local start also `Started.stop()`). A new session or `--fork` never
attaches.

Attaching gives `-p` a real use: `tau serve -d` an agent once, then `tau -p --session NAME "status?"`
from cron or another terminal.

### 9. Flags from 0.15

| 0.15 | 0.16 | why |
|---|---|---|
| `-p/--print`, `MESSAGES...` | kept | words joined with spaces, not newlines; stdin added (§1) |
| `@file` in a message | moved | the `@file` standard extension (8E) expands it; `-p` sends text as typed |
| `--mode text` | dropped | the default |
| `--mode json` | renamed `--json` | as `tau sessions`, `tau serve`, `tau ext list`; the wire's events (§4) |
| `--mode rpc` | replaced | `tau serve` (D21); stdio is Tectum's, after 0.16.0 |
| `--mode repl` | dropped | plan, 2026-11-01 |
| `-m/--model` | kept | a new session only (D18); `/model` switches a running one |
| `--provider`, `--thinking` | dropped | the model's config and its provider `options` (D13, D15) |
| `-t`, `-nt`, `-nbt`, `-xt` | deferred | tools come from extensions (`load`, `-e`, `--no-extensions`); a per-run tool list is Tectum's `--no-tools`, after 0.16.0 |
| `-e`, `--no-extensions` | kept | D19; `-ne` dropped (D19 has the long form only) |
| `--bus` | dropped | the NATS bus is post-0.16 |
| `--max-turns N`, `--ext-config` | replaced | `StepLimit`'s option; per run, Open 4's `--set` |
| `--ui-defaults METHOD=ANSWER` | renamed `--answer EXTENSION[:TEXT]=VALUE` | matches who asked and what, not a dialog kind; no rule declines (§5) |
| `--system-prompt`, `--append-system-prompt`, `-nc` | kept | D20 |
| `--prompt-template`, `--skill` and their `--no-` forms | moved | the skills and templates extension's options (8E) |
| `-c`, `--session`, `--fork`, `-n`, `--no-session`, `--session-dir` | kept | D18; no longer `-p`-only |
| `-r` | kept, refused with `-p` | D18 |
| `--store` | dropped | JMFTS is after 0.16.0 |
| `--connect ADDR`, `--cwd PATH` | dropped | attaching is automatic (§8); `cd` sets the directory |
| `--import-session`, `--export-session`, `--export-html` | post-0.16 / `tau export` | D18 §8 |
| `--verbose` | kept, redefined | §7 |
| `-v/--version` | kept | prints the protocol too |
| `--fun`/`--no-fun` | dropped | plan |

## Gaps this needs filled

1. **`OpenQuestion.extension: str`**, the name of the extension whose handle asked. The driver
   has it (a `Cursor` is bound to its extension; a tool hook's `NodeHandle` too). Without it a
   rule can match only on text, and a free-text question would take any value. A field on an
   event is a wire change: one line in the schema, before the protocol's first release, so no
   `PROTOCOL` bump (D21 §8: 0.16.0 ships 1).
2. **The first cursor without a `Session`** (§8 step 4). Today's function takes the session, so a
   remote client cannot use it. `first_cursor` moves to `open_client`, computed from the snapshot.
3. **The start has no attach outcome.** `open_client` wraps it (§8); `start_session` is unchanged.
4. **`Started` keeps too little for `--verbose`**: the loaded extensions' sources and the prompt's
   parts are computed by the start and dropped. `Started` gains `sources` and `prompt`.
5. **`Welcome.session` becomes `str | None`**, for `--no-session` (§4). A server never sends
   `null`.

## Alternatives

| option | why not |
|---|---|
| Stream `Live` text to stdout (0.15) | A full buffer drops `Live`; a pipe would get a hole and no error. `--json` streams honestly, with the node after. |
| Print every reply, concatenated (0.15) | `$(tau -p ...)` would hold the model's narration. `--verbose` shows it on stderr. |
| A `-p` JSON format of its own (0.15's bus events) | A third shape beside the wire and the log; clients already parse `$defs/Event`. |
| Match `--answer` by dialog kind (0.15) | There are no dialog kinds: a question is text and a schema. Who asked is what a person means. |
| No rule: fail the run | The plan says decline, and D16 §3 makes declining safe. Each decline is on stderr, so nothing is hidden. |
| `-p` never attaches (0.15) | Then a served session cannot be scripted from the shell at all; `Leased` would say "use `tau serve`" for a session that is already served. |
| `-p` asks trust and model questions on the terminal | Its stdout is the answer; a one-shot program should fail with the fix, not stop for input. |

## Open questions

1. **Streaming in a terminal.** When stdout is a terminal, stream `Live` text and then reprint
   the node if it differs? *Recommend:* no. One rule for terminals and pipes; `--verbose` gives
   progress, and the TUI is the streaming frontend.
2. **The final reply only, or every reply.** *Recommend:* the final reply on stdout, the others
   under `--verbose` on stderr (§3). The alternative is 0.15's every reply, separated by blank
   lines.
3. **Questions while attached.** Other clients (a person in the TUI) may be connected, so a
   decline by `-p` races a person's answer. *Recommend:* attached, `-p` answers by rule on any
   cursor but declines only on its own cursor; a question elsewhere that no rule covers is left
   to the server's other clients, and `-p` says on stderr that it is waiting for it.
4. **A per-run options flag.** `--set EXTENSION.FIELD=VALUE` (repeatable), the CLI layer of the
   config (D13) for this run only, parsed as `tau config set` parses it. It replaces
   `--max-turns` (`--set step_limit.limit=5`) and `--ext-config`, for `-p`, `tau` and `tau serve`.
   *Recommend:* yes, in 8B; it is a few lines over code that exists.
5. **`-v` for `--version`.** *Recommend:* keep 0.15's `-v`; `--verbose` stays long-only.

## Glossary entries proposed

- **print mode, `tau -p`**: the one-shot client of §1–§6; **rest** (of a run): the first
  `CursorView` of the cursor, after the call, that is not busy.
- **answer rule, `--answer`**: `EXTENSION[:TEXT]=VALUE`; no match declines.
- **`OpenQuestion.extension`** (amended **OpenQuestion**): the asking extension.
- **`open_client`, `Opened`**: open a session for a client: attach or start, then the snapshot
  and the first cursor; **attach**: connecting to the `tau serve` that holds the chosen session
  instead of leasing it.
- **start report**: what `--verbose` prints before the first message; `Started.sources`,
  `Started.prompt` (amended **shared start**).
- **exit status 3**: stopped before finishing.
- Amended: **first cursor** (computed from the snapshot), **Welcome** (`session` may be `null`).

## Build (rough lines, tests separate)

| item | lines |
|---|---|
| launch parser (first-argument rule, `-p`, `-m`, `--json`, `--answer`, `--verbose`, `-v`); message and stdin | ~120 |
| `open_client`, `Opened`, attach, first cursor from the snapshot; `Started.sources`/`prompt` | ~150 |
| the run: send or command line, rest, questions and rules, exit status, SIGINT | ~200 |
| text and JSON output; the start report and `--verbose` lines | ~120 |
| `OpenQuestion.extension` in the driver, schema and stub | ~20 |
| tests through `tau.cli.main` with `FakeProvider` (each exit status, rules, decline, JSON lines decode with `decode_event`, stdin, attach to an in-process `Server`) | ~400 |

**Not verified, inferred:** that the view `send` publishes is always busy when the cursor was
already busy from a load wake (it should be: the item is work); the line counts.
