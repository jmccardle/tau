# D22: `tau -p`: the one-shot client

Status: accepted 2026-11-02; built in stage 8B (see *As built* at the end). Phase 8, stage 8B. Depends on D16 (clients, questions), D18 §5 (the
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
  terminal, a blank line and everything read from stdin: `git diff | tau -p "review this"`. How
  stdin is read is *As built*, "Standard input and output". Nothing in either place is a usage
  error (exit 2), checked before the start, so it creates no session.
- **Flags** are D18's session flags, D19's `-e` and `--no-extensions`, D20's prompt flags, and
  `-m/--model`, `--json`, `--answer RULE` (§5), `--set EXTENSION.FIELD=VALUE` (§1), `--verbose`
  (§7). `-r` with `-p` is a usage error (D18). `--no-extensions` skips the discovered sources
  (packages, user, project) and leaves every built-in as `load` says, `base_tools` included
  (D19 §5); only `load: {base_tools: false}` turns the tools off.
- **Per-run options.** `--set EXTENSION.FIELD=VALUE`, repeatable, is the CLI layer of the
  config (D13) for this run only, parsed as `tau config set` parses it. It replaces 0.15's
  `--max-turns` (`--set step_limit.limit=5`) and `--ext-config`, and is a flag of `-p`, `tau`
  and `tau serve` alike.
- **No prompts.** `-p` passes no `prompts` to the start: an untrusted project fails with "run
  `tau trust`", and an ambiguous model fails listing the choices. A one-shot program asks nobody
  on its stdout.

### 2. The run

1. Open the session (§8: start it, or attach to the server that holds it). The client id is
   `<user>@<host>:print`.
2. Take the first cursor (D18 §5). Under `--json`, `watch` only it (the text output never uses `Live`).
3. Send the message: `send(cursor, text)`. A message that starts with `/` is a command line
   instead: `parse_line`; an `UnknownCommand` is sent as text with a warning on stderr (as every
   built-in frontend does); missing required arguments are a usage error naming them; otherwise
   `run` it, in a task, so its questions are answered (§5) while it runs.
4. **Wait for rest:** the first `CursorView` of the cursor, after the call returned, that is not
   busy. A `send` wakes the cursor, so the view that call published is busy (D21 §3's order holds
   locally too). Meanwhile answer questions (§5).
5. Print (§3, §4), close the client, stop, and exit (§6). SIGINT aborts the cursor, waits for it
   to rest, stops and exits 130; a second SIGINT exits 130 at once. If the run's message has no
   reply node yet (the `User` node is the only node the run added), the interrupt takes it back so
   the next `-c` does not send two user messages in a row (8E-5): nodes are written once, so the
   node stays in the tree as a dead branch and the cursor goes to the node before it (a message
   still pending is removed from the scratchpad instead). Once the model has replied, even in
   part, nothing is taken back.

`-p` leaves the session as any frontend does: a new session it used is kept and listed.
`--no-session` is the throwaway run.

### 3. Text output

- **stdout is the answer**: the text parts of the **last** `Assistant` node the run added to the
  cursor's walk, if that node makes no calls, printed when the cursor rests (status 0 or 3),
  ending with one newline. Earlier replies ("Let me
  read the file") are progress, not the answer; `--verbose` shows them on stderr. (0.15 printed
  every reply, concatenated.) A run that
  ends without a final reply prints nothing on stdout.
- It prints from the node (`Appended`), not from `Live`. `Live` is a preview a full buffer may
  drop (D16 §5); a pipe must get exactly what the log holds. Streaming text is `--json`'s and the
  TUI's, also when stdout is a terminal.
- **stderr** gets, one line each, prefixed `tau: `: every `Notice`; a `hook_error`; each wait
  before a provider retry (`tau: HTTP 429: busy: retry 1 of 3 in 2 s`, from the own cursor's
  `Retrying`; without `--verbose` too, as 0.15 did); each
  question declined (`... on cursor C: ...`) or answered by a rule (§5); why the run stopped when it did not finish (§6);
  and the failure that ended it.

### 4. JSON output: `--json`

One JSON object per line on stdout, nothing else there, in the wire's own encoding (D21 §4).

1. **First, a `Welcome`**, the type `hello` answers, with `"kind": "Welcome"`: `protocol`,
   `version`, `session` (the id), `cwd`, `pid`. A script keeps the id for `--session`. With
   `--no-session` there is no id, so `-p --json --no-session` prints no `Welcome` line;
   `Welcome.session` stays a non-null string on the wire.
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
  cursor, and the run waits on it. **Attached to a server** (§8), other clients, a person in the
  TUI, may be connected, so `-p` handles only the questions that open after its send, on the
  cursor it sent to or on a cursor opened since it attached (a sub-agent's: the run's own). Every
  other question, one open before the send or on a cursor that was there already, is left to the
  server's other clients, and `-p` says on stderr that it is. It never rules on them.
- **A rule** is `--answer EXTENSION[:TEXT]=VALUE`, repeatable; the first that matches wins.
  It is split at the last `=` (the value), then the rest at the first `:` (extension, text).
  `EXTENSION` is the asking extension's name or `*`. `TEXT` is a glob over the first line of the
  question's text (default `*`). A `permissions` question is `TOOL: CALL` then a line `(ask rule:
  RULE)`: its TEXT is matched against the call alone, with a path tool's paths resolved, and a call
  that chains commands or redirects (`;` `&` `|` a backtick `$(` `<` `>` a newline) is matched only
  by a TEXT that contains the operator (D24 "Built: 8E-4"); a TEXT left out answers every question. `VALUE` is read by the question's schema with `commandline.value`, as a typed
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
  aborts its own cursor (attached, never another client's) and exits 1, naming the rule, the question and the schema's complaint. It
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
cut-off answer from a whole one. Status 3 always comes with a stderr line saying why: a halt's
reason, an abort, the message still pending, or auto-step off. For the last the cursor's view does
not say which of its causes (a step limit, a hook, a step that did not move), so the line names
them all.

`--max-turns N` is `StepLimit`'s option. For one run that is `--set
step_limit.limit=N` (§1).

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
  
  - **what an extension shows** (added 8E-2): a line `tau: NAME: TEXT` whenever the text an
    extension shows on the cursor (`Cursor.show`) differs from the last one, whichever extension it
    is (`tau: usage: 12 in · 4 out · $0.0001 …`). The lines already shown when the run began are
    not repeated.

  It is a report of what happened, not a debug log: no arguments dump, no timings
  (`tau.trace` is the profiler).

### 8. Opening the session, and attaching to `tau serve`

`-p` and the TUI open a session the same way, in one library function, **`open_client(args, *,
client, environ, cwd, ...) -> Opened`**:

1. Resolve the session flags (D18 §5 step 0; writes nothing).
2. **If the choice is an existing session and a live `tau serve` holds it** (its serve record
   answers `hello`, D21 §9), connect a `RemoteClient` to it instead of starting: **attach**.
   `-e`, `--no-extensions`, `--set`, `-n` and `open_client`'s `own=` are then a `ConfigError` (the
   server's extensions, options and name are set; these would be ignored); `-m` and the prompt flags
   are refused already for an existing session (D18, D20). A session held by a process that is not a
   server is still `Leased`, suggesting `--fork`.
3. Otherwise `start_session` and `driver.client(client)`.
4. **Read the snapshot** from the client's stream (every event up to the first `Labels`) and
   pick the first cursor from it, reopening a closed one through the client. This works the same
   on both kinds of client, which `first_cursor(session, client)` cannot (gap 2).

`Opened` holds the `client`, the `cursor`, the `snapshot` events (so the caller loses none; for a
reopened cursor they run up to its view), the `welcome` (a remote client's, or one built from the
start for a local client; none with `--no-session`), the `started` (the local start's `Started`;
`None` when attached) and `close()` (close the client; for a local start also `Started.stop()`). A new session or `--fork` never
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
| `-t`, `-nt`, `-nbt`, `-xt` | deferred | tools come from extensions (`load`, `-e`); `load: {base_tools: false}` turns the base tools off; a per-run tool list is Tectum's `--no-tools`, after 0.16.0 |
| `-e`, `--no-extensions` | kept | D19; `--no-extensions` skips discovered sources only, never built-ins; `-ne` dropped (D19 has the long form only) |
| `--bus` | dropped | the NATS bus is post-0.16 |
| `--max-turns N`, `--ext-config` | replaced | `StepLimit`'s option; per run, `--set` (§1) |
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
   remote client cannot use it. `first_cursor` moves to `open_client`, computed from the snapshot,
   and is no longer a separate public name.
3. **The start has no attach outcome.** `open_client` wraps it (§8); `start_session` is unchanged.
4. **`Started` keeps too little for `--verbose`**: the loaded extensions' sources and the prompt's
   parts are computed by the start and dropped. `Started` gains `sources` and `prompt`.
5. **`--set` needs the extensions loaded** before its values can be read (the option's type decides
   how `5` or `a,b` is read), and the options are part of the config the extensions are loaded
   from. `start_session` loads the config, loads the extensions, reads the `--set` values against
   their options, and loads the config again with them as the `cli` layer (so a bad value fails
   before a session exists). A program takes the flag from `tau.cli.option_flags()`.

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

## Rulings (2026-11-02)

Decided as the drafts recommended, with the changes folded into the sections above.

1. **Streaming in a terminal:** no. One rule for terminals and pipes (§3).
2. **Final reply only** on stdout; the others under `--verbose` on stderr (§3).
3. **Questions while attached:** `-p` answers or declines only questions on its own cursor (§5).
   This is narrower than the draft, which let rules answer on any cursor.
4. **`--set EXTENSION.FIELD=VALUE`:** yes, in 8B (§1).
5. **`-v` is `--version`;** `--verbose` stays long-only (§7).
6. **`--no-extensions`** skips discovered sources only; built-ins, `base_tools` included, stay as
   `load` says (§1, §9).
7. **`Welcome.session`** stays a non-null string; `--no-session` prints no `Welcome` line (§4).
8. **`--answer`** splits the value at the last `=`, then the extension at the first `:` (§5).

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
- Amended: **first cursor** (computed from the snapshot).

## Build (rough lines, tests separate)

| item | lines |
|---|---|
| launch parser (first-argument rule, `-p`, `-m`, `--json`, `--answer`, `--verbose`, `-v`, `--set`); message and stdin | ~120 |
| `open_client`, `Opened`, attach, first cursor from the snapshot; `Started.sources`/`prompt` | ~150 |
| the run: send or command line, rest, questions and rules, exit status, SIGINT | ~200 |
| text and JSON output; the start report and `--verbose` lines | ~120 |
| `OpenQuestion.extension` in the driver, schema and stub | ~20 |
| tests through `tau.cli.main` with `FakeProvider` (each exit status, rules, decline, JSON lines decode with `decode_event`, stdin, attach to an in-process `Server`) | ~400 |

**Not verified, inferred:** that the view `send` publishes is always busy when the cursor was
already busy from a load wake (it should be: the item is work); the line counts.

## As built (2026-11-02, stage 8B)

Where the code differs from the text above, or the text left a choice:

- **Modules.** `tau._launch` (the launch parser, `--answer` rules, the message), `tau._open`
  (`open_client`, `Opened`), `tau._print_mode` (the run). `tau.cli` dispatches on the first
  argument. `tau.cli.option_flags()` is `--set`; `tau.cli.session_flags()` and `extension_flags()`
  are unchanged.
- **`first_cursor` is gone** from the public names; `open_client` is where a frontend gets its
  cursor, local or remote. `first_cursor_node` stays (forks and exports use it without a client).
- **Rest** is found by reading the stream only after `send` returned (the view `send` published is
  busy, and a stream keeps one view per cursor, so no stale idle view precedes it). A command line
  is run while the stream is read, and after it returns the events it published are drained before
  the cursor's state is judged.
- **Status 3 prints the answer** when the last reply is whole (a step limit that turned auto-step
  off after the model's final reply still prints it); only the exit status and the stderr line say
  the run was cut. A last reply that makes calls is not an answer, so a run cut off mid-work
  prints nothing.
- **A command line's result** is printed on stdout (text) with a newline; under `--json`, where
  stdout is events only, it goes to stderr. `/config EXTENSION` with nothing to change is a usage
  error.
  If the command sent input to the cursor (a prompt template does), the reply to it is printed
  after the result, as a message's is, and the run is held to a message's rules: a pending input
  or auto-step off is exit 3 (amended 2026-11-03, 8E-1).
- **`--json` errors.** Every failure that exits 1 ends stdout with `{"error": {"type",
  "message"}}`: a start error by its exception's name, a failed step `Failed`, a refused rule
  `RuleRefused`, a failed command `CommandFailed`, a lost server `ConnectionError`.
- **Questions.** Local, the snapshot's open questions are dealt with too. Attached, `-p` handles
  only the questions that open after its send, on the cursor it sent to (the lowest open cursor,
  which a TUI may share): a question open in the snapshot, or on another cursor, is announced once
  on stderr and left, never answered or declined, and a refused `--answer` value aborts only the
  cursor `-p` sent to. A question `to` another client is left alone. A question that opened
  between the snapshot and the send is taken as opened after it.
- **A rule for an extension this process did not load** is a usage error (exit 2) after the start,
  naming the extensions loaded (not only those that ask: nothing says which do). Attached it is
  not checked: the wire does not carry a server's extension names.
- **A command line on a session `-c` opened** waits first for the cursor's view to rest: loading
  wakes every cursor (model.md Q39), so the step it starts is under way when `-p` sends the
  command, and a command that runs as a step of the cursor would find it busy (8E-5).
- **Second SIGINT** writes `tau: interrupted` and calls `os._exit(130)` at once, running no
  cleanup, since hooks are never cancelled (model.md §8) and an abort waits for the one in flight.
- **Standard input and output** (8E-6). `tau -p` never blocks silently on stdin, and behaves as a
  Unix filter does for each kind:

  | stdin | what happens |
  |---|---|
  | a terminal | not read (unless the line has `-`) |
  | a file, `/dev/null`, a pipe that has sent its data and closed | read to its end at once |
  | a pipe that sends something within 3 s | read to its end, however long that takes |
  | with words: a pipe left open that says nothing for 3 s (cron, ssh, a CI step) | not read: `tau: no standard input within 3 s: going on without it (the word `-` waits for it)` on stderr, and the words go alone |
  | no words at all (`make 2>&1 \| tau -p`) | as for `-`: stdin is the only possible message, so it is waited for; stderr says `tau: waiting for standard input` and that `</dev/null` skips it |
  | the word `-` anywhere in the line | read to its end however long it takes, from a terminal too (Ctrl-D ends it), like `cat -`; after 2 s of silence stderr says `tau: waiting for standard input` once |
  | closed (`<&-`) | empty |
  | not UTF-8 | usage error (exit 2), no traceback |

  Choice: the rule is the wait for the *first byte*, not a guess at whether data is "coming": no
  program can tell an idle pipe from a slow producer, so the default bounds the cost of being
  wrong (3 s and one stderr line) and `-` is the explicit "I mean it" (the `cat -`, `tar -f -`
  convention). `slow-command | tau -p "..."` that is silent for over 3 s needs the `-`. With no
  words the 3 s give-up does not apply (nothing else could be the message): stdin is waited for as with `-`, with the notice. The `-` word is removed
  from the message.

  Standard output: a closed descriptor (`>&-`) is exit 1, `tau: standard output is closed`; a
  reader that went away (`| head -1`), at a write or at the final flush, of any command, is exit
  141 with no message, and the descriptor is pointed at /dev/null so the interpreter's own flush
  at exit stays quiet; a write that fails otherwise (a full disk) is exit 1 with one line.
- **Retries** are told on stderr, one line each, with or without `--verbose` (`tau: HTTP 429: busy:
  retry 1 of 3 in 2 s`); `--json` also carries them as `Live` events.
- **Interrupt.** If the first SIGINT's stop is still waiting for a hook after 2 s, stderr says so
  and that a second SIGINT leaves the message in the session. The second leaves at once
  (above), so a message already written stays: the next `-c` then sees two user messages in a row
  (the first has no reply). Taking it back needs the move to be applied by the core, which a
  hook still running prevents.
- **Tracebacks.** A failed hook, tool or command prints its traceback to the process's stderr
  (`describe_failure`); under `-p` that stream is /dev/null unless `--verbose`, so stderr holds
  `tau: ` lines. This also hides anything else an extension prints to stderr during the run.
- **`--json` and usage errors** also end with the `{"error": ...}` line (`UsageError`).
- **Known:** `tau serve` removes its socket before its record on shutdown, so a `-p` in that
  instant may see a stale record (it then starts the session itself or fails `Leased`).
- **A message of only white space**, in the words and on stdin, is nothing to send (exit 2).
- **`--version`** prints the installed package's version, which is `0.16.0.dev0` in development.
- **`--verbose` for `tau serve`** writes the start report on stderr, which a detached server's log
  collects. For an attached `-p` the report is the server's `Welcome` and the cursor's model.
