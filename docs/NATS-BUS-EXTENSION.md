# The NATS bus extension (`nats_bus.py`, tau-007)

**Status:** written 2026-08-10. This doc did not exist before — the extension
itself (`tau_agent_core/extensions_builtin/nats_bus.py`, 843 lines, rewritten
2026-07-29 against tectum's real implementation, revised again 2026-08-01 for
a second consumer) was flagged in `ROADMAP.md` as a shipped, undocumented
subsystem. Everything below is read from that file, `docs/WIRE-CONTRACT.md`,
and the one real runner that drives it, `scripts/tectum_responder.py`.

**Scope split with the other bus docs.** This page covers the extension as a
piece of τ — how to load it, what it registers, what its config means. The
wire format it speaks (the `TectumEvent` envelope, subject shapes, ack
payloads) is `docs/WIRE-CONTRACT.md`'s job, not restated here. General
extension mechanics (`api.on`, hooks, discovery) are `docs/extensions.md`'s
job. `docs/PI-RPC-REPLACEMENT.md` §3.6 is the design argument for why this
extension exists at all — per-agent credential custody that a `bash`-only pi
agent structurally cannot offer.

## What it is

τ speaking NATS directly, in both directions, standing in as a bus-native
agent node rather than sitting behind tectum's per-dispatch `pi` subprocess.
It has two independent, confirmed-against-real-code consumers:

- **tectum's effector nodes** — `speak`, `journal_append`, `jmfts_write`,
  `delegate`.
- **McRogueFace's body node** (a NATS client running inside the simulation
  engine process) — `move_to`, `wait`, `note`.

Both sides publish and ack `TectumEvent` envelopes on the same wire; the
extension does not import either project's code (τ is a standalone pip
package, tectum and McRogueFace are separate processes) — it re-implements the
envelope as a plain dict, field for field.

## Loading it

`TOUCHES_BUS = True` on the module gates it: the loader refuses to load this
extension into a session that was not explicitly built to allow a bus. Two
ways to grant that:

- CLI: `tau --bus --extension path/to/nats_bus.py --ext-config
  nats_bus.workspace=responder --ext-config
  nats_bus.inbound_subject=events.sensation.audio.resolved.clean ...`
- SDK: `create_agent_session(..., bus_available=True)`, then
  `session.load_extensions([path], discover=False, extensions_config={"nats_bus": {...}})`
  — the real pattern in `scripts/tectum_responder.py`, the only end-to-end
  runner that exercises this extension today. Read that script over
  hand-sketching a config — it is a real, working `responder` node against a
  live NATS bus and a local llama.cpp model.

There is no auto-discovery: `extensions_builtin/` is not on the default
`~/.tau/extensions/` search path. This file is loaded the same way any
user-dropped extension is, using `Path(__file__)` as an explicit entry point
— shipping it as real code (not just documentation) keeps it exercised by
`tau-agent-core/tests/test_nats_bus_extension.py` instead of drifting from
whatever the loader actually does.

## Config (`~/.tau/config.json` → `extensions.nats_bus`, or `--ext-config`)

| Key | Required | Default | Meaning |
|---|---|---|---|
| `workspace` | **yes** | — | This session's agent identity; publishes on `events.workspace.<workspace>.out.<verb>`. No default — an unnamed workspace makes every outbound subject wrong, quietly. |
| `inbound_subject` | **yes** | — | Which subject drives a turn. A property of the active tectum schema (e.g. `events.sensation.audio.resolved.clean`), not something this file may guess — a wrong value never fires, and never fires *loudly*. |
| `draft_subject` | no | none | A second inbound rail (e.g. `events.sensation.audio.partial`) painted on the TUI status strip via `api.ui.set_status("hearing", text)` instead of driving a turn. Gated on `ExtensionUI.interactive` — a headless run skips even parsing it. |
| `nats_url` | no | `nats://127.0.0.1:4222` | |
| `origin_node` | no | `"tau"` | |
| `ack_timeout_s` | no | `30.0` | How long an outbound effector call waits for its ack before raising. |
| `verbs` | no | `("speak",)` | Which effector tools to register — see the table below. |

All three `ValueError`s (`workspace` missing, `inbound_subject` missing, an
unknown verb in `verbs`) are Fail-Early by design, stated in the extension's
own docstring: a guessed value here is wrong in a way that fails silently
later, so it fails loudly at `register()` instead.

## Inbound: one turn per committed event

`session_start` connects to NATS and subscribes `inbound_subject`;
`session_shutdown` unsubscribes and closes the connection. Each inbound
message drives one agent turn via `api.submit(text, multitask_strategy="reject",
correlation=...)` — a refused turn (one already in flight) is reported on
`ext:nats_bus:inbound_dropped` with the submission's own `rejection_reason`,
not silently swallowed. The raw event is also re-emitted on
`ext:nats_bus:inbound` for anything that wants to observe without driving a
turn (a test, a monitor).

`draft_subject`, if configured, is a separate thing: it never calls
`api.submit`. It exists because an ASR partial (`audio.stt`'s revisable
in-flight hypothesis, one event per growing guess, same `binding_id` for the
whole utterance) should be shown, not committed — the committed turn on
`inbound_subject` clears the status slot when it lands.

## Outbound: one tool per verb, each with its own schema

Before this extension's 2026-08-01 revision, every registered tool shared one
`{"text": string}` schema, which made `move_to` (an `{x, y}` verb)
inexpressible. `VERBS` now gives each verb its own JSON Schema, description,
ack contract, and turn-ending behavior:

| Verb | Consumer | Ack subject | Terminal | Notes |
|---|---|---|---|---|
| `speak` | tectum | `events.action.speech.completed.{binding_id}` | yes | An empty string still acks cleanly — silence that looks like speech — so emptiness is rejected before publish (`VerbSpec.non_empty`), not left to the schema. |
| `journal_append` | tectum | `events.journal.append.{binding_id}` | no | Ack subject's kind token is `append`, not the tool name. |
| `jmfts_write` | tectum | `events.journal.jmfts_write.{binding_id}` | no | |
| `delegate` | tectum | none (fire-and-forget) | no | The curator's answer arrives later as a separate `posted` event, not this ack. |
| `move_to` | McRogueFace | `events.journal.move_to.{binding_id}` | no | `x`/`y` integers; reachability is the engine's fact, not τ's — an unreachable cell comes back as an ack failure, not a schema rejection. |
| `wait` | McRogueFace | `events.journal.wait.{binding_id}` | no | `turns >= 1` is not schema-expressible (τ's validator has no `minimum`) and is enforced engine-side. |
| `note` | McRogueFace | none (fire-and-forget) | no | The body node records it without acking — waiting on an ack here would time out every call. |

**Why `terminal` matters, concretely.** An earlier version returned the raw
transport ack as the tool result for `speak`. A live run looped: the model
read its own sentence quoted back in the ack blob, saw no signal that the
turn was over, and called `speak` again — 28 turns before being killed. The
fix is two-part and both halves are load-bearing: `terminal=True` stops the
loop mechanically (`AgentToolResult.terminate`), and `VerbSpec.result` tells
the model in words that the turn is over, so it does not simply retry. The
raw ack is still visible to an operator on `tool_execution_end` — only the
model's context loses it.

**The ack-wait contract ("zero orphans", tau-002).** For any verb with an ack
subject, the tool subscribes that subject *before* publishing (a fast ack
racing the subscribe would otherwise be lost), then polls for the first
message, checking the tool's `AbortSignal` every 0.2s so an aborted turn
doesn't park the coroutine, bounded by `ack_timeout_s`. An ack whose payload
says `ok: False` or carries a non-null `error` raises, same as a timeout —
this file's stated rule is that a mutation either completes or is loudly
reported, never silently assumed. The two consumers disagree on the failure
shape (tectum: `ok`/`error`; McRogueFace: `status: ok|refused|error`) and
`_ack_failure` reads both dialects, because reading only one made this side
blind to the other's failures.

## What it deliberately does not build

`Attribution` (the record of who-did-what for provenance) is not constructed
here — four of its ten fields belong to layers this extension isn't (model
identity, world tick, JMFTS visibility watermark), and there is no call site
yet to record one into. The whole ack payload already rides the
`tool_execution_end` event, so nothing is lost by declining to build the
dataclass — a caller that does have a recorder loses nothing.

No JMFTS call is made from this file, so the "no blocking call inside a
coroutine" concern (relevant once a JMFTS-backed tool joins a bus session)
has no instance here yet — whoever adds that tool inherits the obligation to
keep it off this extension's event loop, since the NATS client's own
heartbeats run on it.

## Related

`docs/WIRE-CONTRACT.md` (envelope and subject shapes, verified end to end
against a real `parley-nats` bridge), `docs/extensions.md` (general extension
API), `docs/PI-RPC-REPLACEMENT.md` §3.6 (the credential-custody argument this
extension is evidence for), `scripts/tectum_responder.py` (the real runner).
