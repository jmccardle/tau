# Replacing `pi --mode rpc` with τ

**Status:** promoted from `.archive/` (gitignored, invisible to git) into
tracked `docs/` on 2026-08-10 — no content change on promotion. It was written
as a blocker analysis, then carried two later `τ →` review passes (2026-08-06,
branch `rpc/tier-b`) tracking each blocker to closed/moot as the real RPC work
landed; see `docs/REMOTE-CONTROL.md` for the shipped design of record and
`docs/RPC-PROTOCOL.md` for the generated wire reference. This document's own
value now is narrower and durable: it is the actual `TauRpcBackend` porting
guide for tectum (§1.3's table is the verb-by-verb mapping), and it names the
one still-open item, §3.3's per-dispatch thinking toggle, which belongs to
`tau-llm`/`tau-agent-core`, not to this integration.

What tectum actually asks of an agent backend, and what τ would have to grow to
answer. Written from the τ side: the contract below is not a wish-list, it is a
transcription of code that runs today.

**Provenance.** Every claim here was read out of source, not inferred.
τ facts are from this checkout — branch `sim/integration`, `9cf92f5`. That
matters: the copy on `midlife` (`7c72173`) differs, and at least one blocker
below is *less* severe here than there (`rpc.py` has tests on this branch and
not on that one). tectum facts are from `/home/john/Development/tectum` at
`d212c5e`. Where something was not verified, it says so.

---

## Review pass — 2026-08-06, branch `rpc/tier-b` @ `ef5b4e1`

> Commentary added after the RPC work landed. Blocks marked **`τ →`** are this
> pass; everything else is the original document, unedited. Original τ facts
> were read at `sim/integration` `9cf92f5`; the RPC surface has since been built
> across phases 2–4 and Tier B, three review rounds deep. **tectum facts were
> NOT re-read** — they are still as of `d212c5e`, so every claim about what
> tectum does remains the original author's and may have moved.
>
> **Headline: the two High blockers are gone, and the Medium/irreversible one
> was bought back rather than accepted.** This document's own recommendation —
> "reconcile the RPC transport rather than embed" — is the path that was taken,
> and it turned out to be the one that also retires §3.5.
>
> | § | Blocker | Then | Now |
> |---|---|---|---|
> | 3.1 | No reachable RPC transport | High | **Closed** — `tau --mode rpc`, 20 live verbs, 7 explicitly declined with reasons |
> | 3.2 | No `new_session` | High | **Closed** — `new_session`, plus `fork` and `switch_session` the doc never asked for |
> | 3.3 | Per-dispatch thinking toggle | Low | **Open, and out of scope** — see the note there; τ has the *lever*, not pi's *mechanism* |
> | 3.4 | Scan-safety | Low | **Moot on the RPC path** — a subprocess imports nothing into tectum |
> | 3.5 | Loss of hard kill | Medium, irreversible | **Not incurred** — the process boundary is back, and its teardown is measured |
>
> What is *not* satisfied, stated plainly and up front:
>
> 1. **The wire shape is JSON-RPC 2.0, not pi's `{"id","type",…}`.** This was
>    never going to be a drop-in and the document says so. `PiRpcBackend` cannot
>    point at τ; tectum needs a sibling `TauRpcBackend`. The framing (NDJSON,
>    caller-allocated ids, dual completion on `prompt`) is the same shape, so
>    that class is a translation layer, not a redesign. `--rpc-dialect pi` is
>    specified as a stated-expiry compat shim (G8) and **does not exist**.
> 2. **§3.3's `chat_template_kwargs.enable_thinking` still has no path.**
>    `AgentLoopConfig` remains a closed field set with no body pass-through. The
>    RPC work made this *reachable a different way* (see §3.3's note) but did not
>    do what the document recommended.
> 3. **Everything in §1.2 is unchanged** — `PATH` shims, `bash`, env inheritance.
>    Nothing in this project touched them, and the RPC path preserves the process
>    boundary that made them work in the first place.

---

## 1. The contract, as tectum uses it

The consumer is `tectum/agent_pool.py`, class `PiRpcBackend` (`:85-268`). One
`pi` process per dispatch; the dispatcher bounds the whole thing with
`max_wall_s` (default 60 s) and enforces "closeout discipline" — if the agent
finished without making its required tool calls, it is re-prompted.

### 1.1 Process spawn

`agent_pool.py:134-146`:

```
pi --mode rpc
   --provider <cfg.provider>          # "local-moe" — the MoE on :8080
   --model <cfg.model>                # "qwen36-35B-IQ4_XS.gguf"
   --thinking <cfg.thinking>          # "off" | anything else — see §3.3
   --system-prompt <path>
   --append-system-prompt <overlay>   # generated tool overlay
   --no-session
   --tools bash
```

stdin/stdout/stderr all piped. stderr is drained and discarded
(`_drain_stderr`, `:150-153`).

**`limit=16 * 1024 * 1024` on the subprocess** (`:92`, `:144`). This is not
tuning. asyncio's `StreamReader` caps lines at 64 KiB; a single `agent_end`
bundling the whole message array routinely exceeds it, and the resulting
`LimitOverrunError` kills the reader task *silently* mid-read. Any replacement
carrying whole message arrays on one line inherits this hazard.

> **`τ →` Satisfied, and this paragraph turned out to be load-bearing in both
> directions.** τ inherits the hazard exactly as predicted, and it is now
> answered on both sides of the pipe rather than left to the host to discover.
>
> *Inbound* (host → τ): the reader no longer uses `readline()` at all — it
> frames its own lines over `read()`, so `StreamReader`'s 64 KiB limit is no
> longer a ceiling on request size. The real bound is published:
> `get_capabilities().limits.max_request_line_bytes` = **8 MiB**, the same
> integer the reader enforces (read live off the constant, so the document and
> the enforcement cannot drift). An over-long line is refused with
> `-32003 REQUEST_TOO_LARGE` — `id: null`, `data` carrying the bound and the
> observed length — and *discarded through its next LF*, so the connection
> resynchronizes instead of dying. A reviewer streamed **13 GiB with no LF**:
> RSS grew 0.6 MiB, exactly one error was emitted, the first LF resynchronized,
> the next request was served, exit 0.
>
> *Outbound* (τ → host): this is the half tectum's `limit=16MiB` exists for, and
> the generated reference now states it. τ's own `get_capabilities` response is
> **67,458 bytes** — measured, not asserted, with a test that goes red if the
> document ever shrinks back under 64 KiB and makes the published prose false —
> and that is the verb version negotiation tells every host to send *first*.
> `get_messages` has no ceiling at all. So a host that keeps the stdlib default
> dies on its first request, and the reference says so rather than leaving it to
> be found. **tectum's `limit=16 * 1024 * 1024` should be carried over verbatim.**

### 1.2 Environment — how the agent acts on the world

`agent_pool.py:124-133` builds the child env:

| var | purpose |
|---|---|
| `PATH` | **`shim_dir` prepended** — this is the whole action mechanism |
| `TECTUM_AGENT` | which agent is running |
| `TECTUM_BINDING_ID` | per-dispatch correlation id |
| `TECTUM_BINDING_FILE` | per-turn re-stamp channel for pooled sessions |
| `NATS_URL` | the bus the shims publish to |

plus `**os.environ` inherited wholesale.

The agent has exactly one tool: `bash`. It acts by invoking generated shell
shims that happen to be first on `PATH`; each shim publishes
`events.workspace.<agent>.out.<tool>` to NATS. `tectum/tools.py:337-341` states
it to the model outright: *"You act in the world ONLY by running shell
commands."*

**τ's `bash` tool ports this unchanged** — `tools/bash.py:106-110` calls
`create_subprocess_shell` with no `env=`, so it inherits the parent
environment. `tectum/tools.py` needs no modification. *(Verified.)*

One wrinkle that only appears in-process: today `PATH` is set in the **child's**
env dict, never in `os.environ`. An in-process τ has no child to configure, so
the shim dir would have to go on the *node process's* own `PATH` — polluting
the node globally, and, with more than one agent node per process, ambiguously.
Nobody has had to solve this yet because the process boundary solved it.

> **`τ →` Nothing here changed, and the wrinkle never had to be solved.** The
> RPC path keeps the process boundary, so `PATH` stays in the child's env dict
> exactly as it is today and `tectum/tools.py` still needs no modification. The
> shim mechanism, `NATS_URL`, the `TECTUM_*` correlation vars and wholesale
> `os.environ` inheritance all carry over verbatim — `tau --mode rpc` is spawned
> the same way `pi --mode rpc` is.
>
> The one thing to decide is whether the phone agent *should* have `bash` at
> all, which is §3.6's argument and is a policy question, not a mechanism one.

### 1.3 Wire protocol

Newline-delimited JSON both ways. Request ids are caller-allocated monotonic
decimal strings (`_alloc_id`, `:199-201`).

**Outbound (tectum → pi)** — three verbs, that is all tectum uses:

```json
{"id":"1","type":"prompt","message":"<text>","streamingBehavior":"steer"}
{"id":"2","type":"abort"}
{"id":"3","type":"new_session"}
```

**Inbound (pi → tectum)** — three message types are recognised; everything else
is ignored (`_reader_loop`, `:155-197`):

```json
{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"…"}}
{"type":"agent_end","messages":[…]}
{"type":"response","id":"1","success":true,"data":{…}}
```

Semantics tectum depends on:

- `message_update` / `text_delta` accumulates the reply. `agent_end` ends the
  turn; if no deltas arrived, tectum falls back to scraping assistant text out
  of `agent_end.messages[]` (`:172-179`).
- `response` resolves the pending future for that `id`. A `prompt` therefore
  has **two** completions — an immediate `response` acknowledging acceptance,
  then an `agent_end` when the loop finishes. `prompt()` waits for both
  (`:220-226`).
- `success: false` on a `response` is an error (`:224-225`).
- `new_session` additionally checks `data.cancelled` — an extension may veto
  the reset, and tectum treats a veto as a hard failure (`:255-256`).

Failure discipline is deliberate and worth preserving: a dead reader unblocks
every waiter with an exception rather than letting each turn hang to its
timeout (`:190-197`), and a mid-turn reader death with no accumulated text
raises rather than returning `""`, which would read as a silently empty
response (`:227-231`).

> **`τ →` Every semantic in this section has an equivalent; the envelope
> differs.** Point by point, because this is the section a `TauRpcBackend` gets
> written from:
>
> | tectum expects | τ gives |
> |---|---|
> | `{"id":"1","type":"prompt","message":…}` | `{"jsonrpc":"2.0","id":1,"method":"prompt","params":{"text":…}}` — **ids are ints, not decimal strings** |
> | `streamingBehavior:"steer"` | `params.multitask_strategy` on `submit`/`prompt` (per-call, not a mode) |
> | `{"type":"message_update","assistantMessageEvent":{"type":"text_delta",…}}` | a `event` notification carrying a `WireEvent`; `message_update` is one of ten types, and the delta is inside it |
> | `{"type":"agent_end","messages":[…]}` | the `agent_end` `WireEvent` |
> | `{"type":"response","id":"1","success":true,"data":{…}}` | JSON-RPC `{"id":1,"result":{…}}` / `{"id":1,"error":{"code",…}}` — **`success:false` becomes an error object with a code**, which is strictly more information |
> | `data.cancelled` veto on `new_session` | `new_session` result is `{cancelled, session, session_id, cursor, store}`; `cancelled: true` means an extension vetoed via `session_before_switch`. **Same field, same meaning.** |
>
> **Dual completion is preserved deliberately and by name.** `submit`/`prompt`
> answer twice — an acceptance response, then `agent_end` — and the reference
> calls this C3 and cites *this document* as the precedent. A rejected
> submission errors on the response instead (`-32000 SUBMISSION_REJECTED`) with
> no later event, which is the honest version of `success:false`. One verb
> beyond the pi three also answers twice (`compact`, via a `compaction_end`
> notification); nothing else does, and the set is pinned by a test.
>
> **The failure discipline this paragraph asks to preserve is tectum-side and
> nothing here threatens it.** The τ-side half is better than it was: a dead
> child is a dead child, but a *live* child now cannot silently swallow a
> request — a corrupt line, an over-long line, a bad `params` shape and an
> unknown method all produce a coded error response and leave the connection
> serving. There is no "ignored silently" path left on the inbound side.
>
> **One thing to know that pi did not make you think about.** The
> `compaction_end` notification is **not** an `event` — it is its own method
> with its own params shape. A translation layer that treats every non-`event`
> notification as a protocol violation will drop it. Irrelevant if tectum never
> calls `compact`; a silent hang if it does.

### 1.4 The three operations, and why each exists

**`prompt(text) -> str`** — the turn.

**`abort()`** — load-bearing, with a scar. From `:234-237`: *"when a caller
times out a turn it MUST abort, or the agent loop keeps running (a tool-looping
model then generates unbounded — observed live: 107 speak calls)."*

**`new_session()`** — the `/clear` equivalent. Clears conversation history while
the **process, system prompt and tools stay warm**. `:243-251` explains the
design it enables: an agent that is *ephemeral in context but pooled in
process*, where every turn derives its whole state from the framed prompt
rather than from a divergent in-session narrative. Four nodes call it per turn:
`responder2.py:167`, `responder3.py:295`, `curator2.py:156`,
`jmfts_operator.py:173`.

**`stop()`** — `terminate()`, then `kill()` after 5 s (`:258-265`).

> **`τ →` All four are present.** `prompt` (and `submit`, its
> provenance-carrying superset), `abort`, `new_session`, and process teardown.
>
> **`abort`'s scar is respected and then some.** The 107-speak-call story is
> exactly the availability property the transport was designed around: the RPC
> reader is strictly serial, so any handler that blocks holds up every later
> request *including `abort`* — the host's only recourse short of a kill. That
> is written into the design docs as the thing not to trade, and it is why
> `compact` acknowledges immediately and reports later rather than awaiting a
> provider call inline. `abort` also now reaches an in-flight compaction, which
> it previously reported success for without stopping.
>
> **`new_session` keeps the process, system prompt, tools and provider client
> warm** — the reset set is defined explicitly (log, cursor, usage, side_usage,
> last-compaction anchor, queued messages, deferred ops, streaming flag) and
> everything off that list survives *on purpose*. That is precisely the
> "ephemeral in context, pooled in process" property §1.4 says the design
> depends on, and it is stated as a contract rather than left as an emergent
> behaviour. `fork` and `switch_session` came along with it; tectum asked for
> neither and may never want them.
>
> **`stop()` needs no change.** SIGTERM/SIGHUP/SIGINT/EOF were all measured
> against a non-reading peer with a 20-second turn in flight: **1.07 s to exit
> on all four**, rc 143/129/0 respectively, nothing hangs. The 5-second
> `kill()` escalation stays worth keeping as a backstop, but it should not be
> firing.

---

## 2. Migration surface in tectum

Smaller than it looks. `backend_factory` is pinned in six nodes —
`responder.py:98`, `responder2.py:87`, `responder3.py:128`, `curator2.py:84`,
`persona_live.py:84`, `jmfts_operator.py:100` — plus the dispatcher default at
`agent_pool.py:300`. `persona_reflection` uses the default and needs nothing.

But note that the `AgentBackend` Protocol does **not** actually describe the
contract. Neither `new_session` nor `abort` is on it; callers reach past it with
`getattr(self.backend, "new_session", None)`. So agent_pool's docstring claim
that the interface absorbs a backend swap is optimistic — the *duck typing*
absorbs it. A τ backend must provide all four operations regardless of what the
Protocol says.

`tests/test_closeout.py` drives a `FakeBackend` and is backend-agnostic.

> **`τ →` Unchanged, and the duck-typing observation is the useful one.** A
> `TauRpcBackend` must supply all four operations regardless of what the
> `AgentBackend` Protocol says, exactly as this section warns. All four exist on
> τ's wire, so that is a mapping exercise rather than a design one.
>
> One addition worth considering while the class is being written: τ publishes
> `get_capabilities().commands[]` and `declined[]`, so a backend *can* assert at
> `start()` that the child it just spawned actually has the verbs it intends to
> call, and fail loudly at spawn instead of on the first `abort` of a runaway
> turn. pi gave no way to ask. That is the sort of thing worth spending ten
> lines on given §1.4's scar.

---

## 3. Blockers

### 3.1 There is no reachable RPC transport

`rpc.py` exists (540 lines, `RPCHandler`, exported from `__init__.py`) and on
this branch it **is tested** — `tau-agent-core/tests/test_rpc.py`, 574 lines.
(On `midlife`'s commit those tests are absent; this branch is ahead.)

It is nonetheless unreachable. `--mode` accepts only `text|json`
(`tau-coding-agent/src/tau_coding_agent/cli.py:141-145`), and `cli.py` contains
no reference to `RPCHandler` at all. Nothing constructs the handler.

And the protocol is different by design. τ speaks JSON-RPC 2.0
(`{"jsonrpc":"2.0","id":1,"method":"send_prompt","params":{…}}`) with verbs
`send_prompt · send_tool_result · abort · get_commands · get_tools ·
get_session_info` (`rpc.py:330-338`). pi's shape is `{"id","type",…}` with
`message_update`/`agent_end`/`response`. `ROADMAP.md:405-411` says so plainly —
Tier 12, *deferred*, and τ's `rpc.py` is **"distinct from pi's `RpcCommand`
protocol; reconcile here."**

So: either wire and reconcile the RPC surface, or go in-process via
`create_agent_session` / `AgentSession.prompt()`. In-process is less work but
costs §3.4 and §3.5.

> **`τ →` CLOSED. The first option was taken.** `tau --mode rpc` exists
> (`cli.py` → `rpc_mode.run_rpc`), speaks JSON-RPC 2.0 over stdio, and ships 20
> live verbs plus 7 explicitly declined ones. The old 540-line `rpc.py` was
> replaced outright, not extended: block-decomposed into transport / dialect /
> command table / event stream / runtime host / process contract / capability
> document, specified in `docs/REMOTE-CONTROL.md` and generated into
> `docs/RPC-PROTOCOL.md` (a *generated* reference — a drift test asserts the
> checked-in file equals `render()`, so the document cannot quietly lie).
>
> **"Reconcile the verbs" was answered by not reconciling them.** τ's surface is
> its own: `submit`/`prompt`/`abort`/`get_state`/`get_messages`/`get_commands`/
> `get_tools`/`get_capabilities`/`new_session`/`fork`/`switch_session` (Tier A/C)
> and `compact`/`set_model`/`get_models`/`set_session_name`/`get_session_name`/
> `get_session_stats`/`list_sessions`/`set_auto_compaction`/
> `get_last_assistant_text` (Tier B). Seven verbs are **declined with a stated
> reason** rather than silently absent, and a host reads that list off
> `get_capabilities().declined[]` — including `send_tool_result` and `bash`,
> both refused on the same ground ("a second privileged path into the same
> executor is a second thing to secure"), and three pi keybinding-shaped verbs
> (`cycle_model`, `cycle_thinking_level`, `set_steering_mode`) that have no
> session state to act on.
>
> **The cost this leaves on tectum's desk is one class.** `PiRpcBackend` cannot
> be pointed at τ. It needs a sibling that speaks JSON-RPC — same NDJSON framing,
> same caller-allocated ids (ints now), same dual completion, different envelope
> and different verb names. The §1.3 table above is the whole mapping.
> `--rpc-dialect pi` is specified as a stated-expiry compat shim and **does not
> exist**; if tectum would rather have that than a new class, it is a real
> option and someone has to build it.

### 3.2 No `new_session`

`AgentSession` exposes `prompt` (`:2457`), `abort` (`:3244`), `set_model`
(`:715`) — and no session-reset verb. `rpc.py`'s handler table has none either.

Constructing a fresh `AgentSession` per turn is *semantically* right but throws
away exactly the warm-process property that made pooling worth doing. This is
the one gap where τ has no partial answer.

> **`τ →` CLOSED, and this section's framing is what the answer was built on.**
> "Ephemeral in context, pooled in process" is quoted in the design docs as the
> requirement, and the fix is not "construct a fresh `AgentSession`" — it is a
> defined reset of the session *log* underneath a session object whose expensive
> parts never move. The **reset set is enumerated**: log, cursor, usage,
> `side_usage`, last-compaction anchor, queued messages, deferred ops, streaming
> flag. Everything else — system prompt, tools, model, extensions, provider
> client — stays warm *by specification*, so a future change that quietly resets
> one of them is a contract violation rather than a performance regression
> nobody notices.
>
> Two things worth knowing before wiring it:
>
> - **The veto is real and typed.** `cancelled: true` when an extension refuses
>   via `session_before_switch`, exactly the `data.cancelled` tectum already
>   checks. Treating it as a hard failure (`:255-256`) remains correct.
> - **A turn in flight is refused, not raced.** `new_session` asks the turn to
>   stop and waits a bounded interval; if the turn does not release the lock it
>   answers `-32002 TURN_STILL_RUNNING` with nothing swapped. Retry, or wait for
>   `agent_end`. It will not hang, and it will not half-swap.
>
> §4's escape hatch — "a new agent can be built fresh-process-per-dispatch,
> which deletes blocker §3.2 outright" — is no longer needed to delete it. The
> phone agent can still be built that way if that is simpler; it just is not
> forced.

### 3.3 The fast/slow lever — smaller than first thought, but real

tectum runs **one** model and switches roles by toggling thinking. pi
implements this via `thinkingFormat: "qwen-chat-template"`, which sets
`chat_template_kwargs.enable_thinking` per request. `DispatchConfig.thinking`
(`agent_pool.py:50-53`) is the whole fast/slow lever.

τ has no `thinkingFormat`. Its reasoning path emits **only**
`reasoning_effort`, gated on `Model.reasoning`, mapped through
`thinking_level_map` (`tau-llm/src/tau_llm/providers/openai.py:1093-1111`). The
one place it touches `chat_template_kwargs` sets it `False` on the
grammar-constraint path (`:265-266`).

**However** — and this corrects a first-pass reading — the provider *does*
accept arbitrary per-call body fields. `openai.py:1046-1066` sweeps every
option key except `api_key`/`reasoning`/`abort_signal`/`constraints` into
`body_options` and merges it into the payload *above* `Model.extra_body`, i.e.
per-call wins. The guards reject only `_RESERVED_BODY_KEYS`
(`model·messages·stream·stream_options·tools`, `:105`) and
`_CONSTRAINT_BODY_KEYS` (`grammar·json_schema·response_format`, `:128`).
`chat_template_kwargs` is in neither.

The break is one layer up. `AgentLoopConfig` (`agent_loop_types.py:71-85`) is a
closed field set, and `agent_loop.py:706-718` builds `options` from exactly
those fields:

```python
options = {"temperature": self.config.temperature}
if self.config.api_key:      options["api_key"] = self.config.api_key
if self.config.reasoning is not None: options["reasoning"] = self.config.reasoning
if self._abort_signal is not None:    options["abort_signal"] = self._abort_signal
```

There is no pass-through, so a caller cannot reach the mechanism the provider
already has.

Three ways out, cheapest first:

1. **Add a body pass-through to `AgentLoopConfig`** and merge it at
   `agent_loop.py:718`. Roughly a field plus a merge, and it lands
   per-dispatch, which is what tectum needs. Recommended.
2. **Static `Model.extra_body`** — two model entries over the same id (fast /
   slow) plus `AgentSession.set_model()`. Legal today, no τ change, but it
   makes a per-request concern into config.
3. **Port `thinkingFormat`** properly into `tau-llm`. Most faithful to pi,
   most work, and the right answer if more qwen-template knobs follow.

Unverified: whether τ has ever spoken to the `:8080` MoE. `~/.tau/config.json`
did not exist on `midlife`. Worth one smoke test before trusting any of this.

> **`τ →` STILL OPEN, and deliberately out of this project's scope.** The
> recommended fix — a body pass-through field on `AgentLoopConfig` merged at
> `agent_loop.py:718` — **was not done**. `AgentLoopConfig` is still a closed
> field set (`model · system_prompt · tool_execution_mode · max_retries ·
> max_turns · temperature · api_key · reasoning`), and the loop still builds
> `options` from exactly those. Everything this section says about the provider
> already accepting arbitrary body fields, and the break being one layer up,
> remains true and unaddressed. This is a `tau-llm`/`agent-core` change; the RPC
> project had no business making it and did not.
>
> What DID change is that **option 2 stopped being "config" and became a wire
> verb.** `set_model` is live (persists the choice and returns a cursor), and
> `get_models` enumerates what the child can be switched to — added because a
> config *name* nobody could enumerate is not a wire contract. So the two-entry
> fast/slow pattern is now per-dispatch and controllable from the host, without
> a respawn:
>
>     get_models              -> ["qwen-fast", "qwen-slow", …]
>     set_model {"name": "qwen-slow"}
>
> That is still option 2, with option 2's cost: a per-request concern expressed
> as two config entries. It is *not* pi's `thinkingFormat`, and if the toggle
> needs to be genuinely per-request rather than per-connection, option 1 is
> still the fix and still unwritten.
>
> `cycle_thinking_level` is **declined**, with the reason stated on the wire:
> τ has no `thinkingLevel` concept on `AgentSession` at all, so there is nothing
> for a `set_*` verb to set. Fixing §3.3 properly would give that verb something
> to mean.
>
> The `:8080` smoke test is still unrun as far as this pass knows.

### 3.4 Scan-safety forbids importing τ at module scope

tectum enforces that node modules import no heavy dependency —
`tests/test_nodes_are_scan_safe.py` runs `scan_nodes()` in a clean subprocess
and asserts none of `torch · whisper · pyannote · transformers · httpx · nats ·
numpy · sounddevice · sentence_transformers` landed in `sys.modules`.

`tau_llm/providers/openai.py:30` imports `httpx` at module top, and
`tau_agent_core/__init__.py` pulls the stack in. Since every agent node imports
`agent_pool` at module top, **any τ import must be deferred inside
`TauBackend.start()`** — no module-level `from tau_agent_core import …`, no
module-level type annotations naming τ types.

Survivable, and `agent_pool.py` is already written this way for other reasons.
But it is a standing constraint, not a one-time fix.

> **`τ →` Moot on the path that was taken.** This blocker is a consequence of
> embedding, and τ is not embedded — `tau --mode rpc` is a subprocess, so
> nothing τ imports ever enters tectum's `sys.modules`. `httpx` at
> `tau_llm/providers/openai.py:30` is now someone else's process's problem.
>
> A `TauRpcBackend` needs no τ import at all: it needs `asyncio.subprocess`,
> `json`, and the verb names. That is a *smaller* dependency than
> `PiRpcBackend` has today. The standing constraint stands, and this backend
> does not test it.

### 3.5 In-process loses the hard kill

`stop()`'s `terminate`/`kill` is an unconditional guarantee against a runaway
model — the thing that ends a 107-speak-call loop. In-process, `abort()` is
*cooperative*: `agent_loop.py` polls the abort signal per SSE line. A model
looping through tool calls, or a tool blocking, or any CPU-bound stretch, now
shares tectum's event loop and degrades the bus for every other node.

This is the only genuinely irreversible loss on the list. It argues for
reconciling the RPC transport (§3.1) rather than embedding, if the appetite
exists.

> **`τ →` NOT INCURRED. The appetite existed.** This paragraph is the argument
> that decided the shape of the whole project, and it was right: the transport
> was reconciled, so the hard kill is never given up. `terminate()`/`kill()`
> works on a τ child for the same reason it works on a pi child.
>
> Better than break-even, in fact. Because the reader is strictly serial and
> nothing unbounded is allowed to run on the dispatch path, `abort` stays
> *answerable* while a turn is in flight — measured: `get_state` answered at
> +0.44 s and `abort` at +0.46 s against a compaction gated on a 20-second
> provider. Cooperative abort still cannot stop a wedged tool, which is why
> `stop()` stays the backstop; but the escalation ladder now has a rung between
> "ask nicely" and "SIGKILL", and the child hits it in ~1 s rather than sitting
> on it.

---

## 3.6 The argument *for* τ that outweighs the list above

Everything to this point is a cost. This is the benefit, and it is not
ergonomic — it is a capability pi structurally cannot provide.

JMFTS already runs a two-principal RBAC posture, provisioned and live
(`jmfts/scripts/setup_agent_principals.py` on `midlife`, tokens minted
2026-08-01):

```
doc 248300  "Kevin Agent Root"           ACR, kevin=write
doc   7231  "Adjutant Application Root"  ACR, adjutant=write
everything else (~19k docs)              ungoverned → shared read/write
```

Two agents, private memory subtrees, one shared knowledgebase. Enforcement is
real (denied reads 404 rather than 403, and are hidden from search counts,
subtree walks and the roots listing — `scripts/check_agent_access.py` asserts
exactly those negatives over live HTTP).

**Under pi this fence is decorative.** pi is launched `--tools bash`
(`tectum/agent_pool.py:140`), so every capability must be reified as an
executable on `PATH`, and any credential that executable needs must live
somewhere the agent's own shell can reach. `tectum/tools.py:307-326` puts it in
`os.environ`, which `agent_pool.py:125` passes to the child wholesale — one
`printenv` away from the model. Moving it to a 0600 file changes nothing: the
agent runs under the same uid as the shim, so it can read the file too. Within
one uid there is no hiding a secret from a process that can run arbitrary
shell. The only real fixes are a *different uid*, or *not giving it bash*.

τ is the second one, without losing the capability. `ToolDefinition.execute` is
an arbitrary `Callable` (`tau-agent-core/src/tau_agent_core/tools/base.py:44`),
registrable from the host application or an extension
(`sdk.py:186-191`). A tectum node — which already holds an in-process
`JMFTSClient` — can register `jmfts_recall`/`jmfts_write` as Python callables
closing over a client constructed with *that agent's* bearer. The model invokes
them by name. The token never enters its context, its environment, or an argv.

So per-agent RBAC is not an application of a τ migration; it is a *reason* for
one. The `kevin`/`adjutant` split exists in JMFTS today and cannot be honoured
by any pi-hosted agent that also has a shell.

Caveat, unchanged from §1.2: τ's own `bash` tool inherits the parent
environment like any subprocess. The custody argument holds only for an agent
whose tool set *excludes* bash, or whose environment carries no secret.

> **`τ →` The argument survives the RPC path, with one wrinkle this section
> could not have anticipated.** Custody works because `ToolDefinition.execute`
> is an arbitrary `Callable` registrable by the host — and on the RPC path the
> "host" is the **τ child process**, not tectum. So the closure holding the
> agent's bearer is constructed inside the child (by an extension, or by
> `--extension`), and the token reaches it through the child's *environment or
> config*, not through tectum's process memory.
>
> That is still strictly better than pi: the token is in the child's env, and
> the model cannot `printenv` it because **the model has no shell**. `bash` is
> a Tier D declined verb precisely so there is no out-of-band path into the
> executor. But it is a weaker statement than the in-process version this
> section imagined — there, the secret never left tectum. Here it crosses one
> process boundary into a process that has no way to read it out.
>
> The caveat's own terms are unchanged and now enforceable: an agent spawned
> `--tools jmfts_recall,jmfts_write` (no `bash`) has no mechanism to disclose
> its own credential. The `kevin`/`adjutant` split is honourable on this path.
> Whoever builds it should confirm the extension-registration route end to end;
> nothing in this project exercised it.

## 4. Assessment

Not a swap — a port. Nothing in τ can serve tectum today without τ-side work.

Ordered by cost:

| # | Blocker | Severity | Shape of fix |
|---|---|---|---|
| 3.1 | No reachable RPC transport | High | Wire `RPCHandler` + reconcile verbs, **or** go in-process |
| 3.2 | No `new_session` | High | New API; no partial answer exists |
| 3.3 | Per-dispatch thinking toggle | **Low** | Pass-through field on `AgentLoopConfig` |
| 3.4 | Scan-safety | Low | Defer all τ imports into `start()` |
| 3.5 | Loss of hard kill | Medium, irreversible | Only the process boundary buys it back |

**The trial vehicle: the phone agent.** tectum is growing a handset rail
(`events.sensation.handset.turn`, `tectum/nodes/audio/handset.py`) that needs an
agent of its own, and that agent is the designated first τ consumer.

It is a better target than any existing node, for a reason worth stating
plainly: **a new agent can be built fresh-process-per-dispatch, which deletes
blocker §3.2 outright.** `new_session` is called only by the four *pooled*
nodes; `persona_reflection` uses the dispatcher default and never touches it. So
a phone agent needs `prompt`, `abort`, `stop` — and nothing τ lacks an answer
for. The hardest gap simply does not apply to a greenfield agent.

What remains for that path: §3.1 (transport, or accept in-process), §3.4 (defer
every τ import into `start()`), and §3.5 (in-process means cooperative abort
only — for a phone agent, with nothing downstream depending on it, that is a
tolerable first exposure). §3.3 is close to moot if the phone picks one thinking
level and keeps it.

Nothing else in tectum moves. The room loop stays on pi, and a failure in the
phone agent cannot disturb it — which is exactly what makes it the right place
to find out what τ is missing.

`tau-jmfts/` is unrelated to this: it stores *τ sessions* in JMFTS
(`SessionLog`/`SessionCatalog` + REST client), sharing only the server with
tectum's own JMFTS use.

---

## 5. `τ →` Assessment, second pass (2026-08-06)

**Still a port, not a swap — but the porting is now almost entirely on
tectum's side, and it is one class.** The document's closing sentence ("Nothing
in τ can serve tectum today without τ-side work") is no longer true. The
τ-side work named in §3.1, §3.2 and §3.5 is done; §3.4 evaporated with the
choice of transport; §3.3 is real, open, and belongs to a different package.

Revised table:

| # | Blocker | Then | Now | What is left, and whose |
|---|---|---|---|---|
| 3.1 | No reachable RPC transport | High | Closed | tectum: a `TauRpcBackend` speaking JSON-RPC. §1.3's table is the mapping. |
| 3.2 | No `new_session` | High | Closed | tectum: check `data.cancelled` (unchanged); handle `-32002` as retry-or-wait. |
| 3.3 | Per-dispatch thinking toggle | Low | **Open** | τ: pass-through on `AgentLoopConfig` (unwritten). Workaround: `get_models` + `set_model` over the wire. |
| 3.4 | Scan-safety | Low | Moot | nobody: a subprocess imports nothing. |
| 3.5 | Loss of hard kill | Medium, irreversible | Not incurred | nobody: the process boundary was kept. |

**Things the original document did not ask for, that a host now gets.** Worth
naming because they change what an integration can *be*, not just whether it
works: `get_capabilities` version negotiation (a host can refuse to run against
an incompatible protocol rather than discovering it on the first failing
request); a generated protocol reference that cannot drift from the code;
published `limits`; `submit` carrying provenance (`source`, `submitter`,
`correlation`, `depth`) so a pooled agent's turns are attributable; and
`fork`/`switch_session`/`list_sessions` for hosts that want session lifecycle
rather than resets.

**Three things to hand whoever writes `TauRpcBackend`:**

1. **Call `get_capabilities` first**, per K2 — and give the subprocess reader a
   16 MiB limit before you do, because that response is 67 KB and the stdlib
   default is 64 KiB. The one failure mode that will bite on line one is the
   one §1.1 already documented for pi.
2. **`prompt` answers twice**, same as pi. A rejected submission is an error
   response with `-32000`, not a `success:false`.
3. **Not every notification is an `event`.** `compaction_end` is its own
   method. Ignore it if you never call `compact`; do not treat it as a protocol
   violation.

**Honest limits of this pass.** tectum was not re-read — every claim about what
tectum does is still as of `d212c5e` and may have moved. Nothing here has been
run against tectum, against the `:8080` MoE, or against a real dispatch; the
measurements quoted are from τ's own conformance suite and review harnesses
driving real `tau --mode rpc` children. The first real integration will find
things this document cannot.

**The phone agent is still the right trial vehicle**, for a better reason than
§4 gives. §4 chose it because a greenfield agent sidesteps `new_session`; that
sidestep is no longer needed. It is the right vehicle now because it is the
place where a failure cannot disturb the room loop — which was always the
stronger half of the argument.
