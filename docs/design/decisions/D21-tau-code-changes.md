# What tau-code changes for D21 (full table, moved out of D21 §10)

tau-code 0.8.1 is built against 0.15's protocol "0.12". Left column: what it does today.
Right column: what it does against D21.

| today (0.12) | with D21 |
|---|---|
| `serve -d --json` prints `{address, pid, started, log}`; dial `ws://address` | prints the serve record, one JSON object on one line; dial `ws+unix://<socket>:/` (the `ws` package supports it). The web host cannot connect until TCP exists (D21 Open 4) |
| one daemon; `list_sessions`, `new_session`, `fork`, `attach`/`detach` | one server per session: `tau sessions --json` to list (D18), `tau serve -d --json <session flags>` to open, create or `--fork`; one connection per session it shows |
| `hello {protocol: "0.12", client, token, suspensions}` | `hello {protocol: 1, client, buffer?}`. `client` is stable per window (kept in webview state), e.g. `john@host:vscode-<n>` |
| replica with `epoch`/`seq`/`since`; node events (`entry_open`, `entry_append`, `entry_final`) with string ids | snapshot on connect (`CursorView` per open cursor, `CursorGone` per closed, `Labels`), `walk`/`nodes` reads, `Appended` with integer node ids; on `end` or a drop, reconnect to a new snapshot |
| `cursors` event | `CursorView`/`CursorGone` per cursor |
| `agent_event` deltas (`message_update` with `replace`) | `Live` with `TextDelta`, `ThinkingDelta`, `CallNamed`, `CallArgs`, `ToolProgress`; always incremental; D15's client rules |
| `request`/`request_closed`, `answer_request`, `answer_suspension`, `answer` | questions in `CursorView.questions`, `answer(question, value)` |
| `ui` notify/status | `Notice`, `CursorView.shown`; panels and warnings are gone |
| `submit {..., multitask_strategy: "enqueue", expand_commands}` | `send` (input waits as an item while the cursor is busy); `/...` via `parse_line`, then `run` or `configure` |
| `surface.commands`, `next_step`, `perform_ready`, `enumerate_domain`, `complete_path` | `commands()`, `parse_line`, `complete_line`, `complete` |
| `set_model`, `navigate`, `get_tree` | `run(cursor, "model", ...)`, `go_to`, `nodes`/`children` |
| `--stop` with `TAUD_TOKEN` | `tau serve --stop` |
| close code 4000; `UNAUTHORIZED` | `end` with `CutOff`; no token |
| generator: `x-result`, `x-data`, `x-names`, `DEFAULT_PORT` | `x-result`, `x-names`, `x-protocol`; `kind`-tagged events and serde-tagged core values; no port. Its emitter needs the new shapes; it keeps failing loudly on unknown keywords |

Also from D18: `list_sessions` rows lose `message_count`, and `forked_from` replaces `parent`.

No wire counterpart: elide, commit-branch, paste-subtree, compare, the extension-panel
verbs, and the step debugger's parked-question verbs. The plan already sends them to
commands or extensions, or drops them.
