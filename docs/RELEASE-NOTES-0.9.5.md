# v0.9.5 — two vendors, a frozen screen, and a loop that could not stop

Written from the commits between `v0.9.4-fullhistory` (`17d0ac0`) and master, so
the release commit, the GitHub release body and the site all say the same thing.

0.9.3's headline was "three vendors". 0.9.4 shipped the TUI work that made a long
session usable. Between the two, nobody ran a tool-using conversation through the
Anthropic or Google clients end to end — and both were broken. One raised a
`TypeError` inside τ before reaching the network; the other sent an empty tool
result to the model and got a 400 back. Both are fixed here, and in both cases
the suite had been green over the defect because the tests agreed with the code
about a shape the wire has never used.

The rest of the release is three smaller things: the tools that froze the screen,
the loop that had nothing left to stop it, and a reference an agent can read.

**Ten commits.** Two provider fixes, one TUI crash fix, one docs mechanism, two
loop/tool changes, and four documentation commits.

---

## The Google client read pi's field names off a τ message

`_append_tool_result` read `toolName`, `toolCallId`, `isError` and `output`.
`ToolResultMessage` (`types.py:408`) declares `tool_name`, `tool_call_id`,
`is_error` and `content`, and carries no aliases, so `model_dump()` emits
snake_case and all four reads resolved `None` on every tool result.

The loud half was a 400 — `function_response.name: Name cannot be empty` — on the
first turn after any tool call. The Google provider could not complete a single
tool-using conversation.

The quiet half was worse. `output` never resolved either, so the request body
that preceded the 400 carried an **empty result for work the tool had actually
done**. Had `name` been optional, there would have been no error at all: the
model would simply have been told its tool returned nothing.

`anthropic.py:482` and `openai.py:1160` read the same message correctly. This was
the odd one out, not a convention the three clients disagreed on.

The fix is two halves, and the second is the one that keeps it fixed. The client
now reads the field names `types.py` declares, and the tests build their fixtures
through `ToolResultMessage.model_dump()` instead of hand-writing camelCase
literals. A `_tool_result()` helper replaces six of those literals, so a rename in
`types.py` now breaks this suite instead of hiding in it. Verified by reverting
the source half against the new tests: 6 of the 46 fail.

Reported, with a fix, by a server running τ against `google-generative-ai`.

## A temperature nobody chose broke every `anthropic-messages` call

The first real call against an Anthropic gateway never reached the network:

```
TypeError: AsyncMessages.stream() got an unexpected keyword argument 'temperature'
```

Three things had to be true at once for a provider this heavily tested to fail on
every request.

**τ sent a temperature nobody chose.** `AgentLoopConfig` defaulted it to `0.7`
and `_build_options` put it in `options` unconditionally. No operator could reach
that value: `Model` had no `temperature` field, so `agent_session`'s
`getattr(self._model, "temperature", 0.7)` always fell through to the literal,
and a `temperature` key written into `~/.tau/config.json` was dropped by pydantic
without a word. pi does not do this — `simple-options.ts:32` forwards
`options?.temperature`, which is undefined unless a caller sets one, and pi's
agent never sets one.

**This provider splats `options` into a typed Python method**, not into a JSON
body. `anthropic` 1.0.0 removed `temperature`/`top_p`/`top_k` from
`messages.stream()` and declares no `**kwargs`, so an unknown key is a
`TypeError` raised inside τ, naming neither the model nor the fix.

**The tests stubbed the SDK's streaming surface**, so the signature change was
invisible to them.

`Model.temperature` is a real field now, defaulting to `None`. τ sends no
temperature and the endpoint applies its own, which is the only answer that can be
right for llama.cpp (0.8), the OpenAI wire (1.0), and a Messages API that removed
the parameter. `AgentLoopConfig.temperature` and `Settings.temperature` follow it
to `None`, `_build_options` includes the key only when it is set, and
`backends.build_model_from_config` reads it — so `models.<name>.temperature` in
`~/.tau/config.json` finally means something.

The Anthropic provider now asks the **installed** SDK what `messages.stream`
accepts (`inspect.signature`, cached against the underlying function) and routes
around what it does not, so it answers SDK drift in its own words instead of by
`TypeError`.

## Textual's own 21 themes stopped crashing

This fix is described in `docs/RELEASE-NOTES-0.9.4.md`, which was extended after
the 0.9.4 tag was cut. It was **not** in 0.9.4. `cf3920a` is not an ancestor of
`v0.9.4-fullhistory`; it ships here. The 0.9.4 notes now carry a line saying so.

`App.__init__` registers every theme in `textual.theme.BUILTIN_THEMES`, and
Textual's "Theme" system command lists all of them. None of those 21 defines the
`$tau-*` variables `parley.tcss` reads, so τ's four worked and picking any of the
other 21 stopped the app with `reference to undefined variable '$tau-bg'`.

`themes.adapt_theme` now derives a palette for a theme that has none, out of the
design tokens it already carries, and `textual_themes()` re-registers each adapted
copy under the same name — so both theme lists in the command palette reach the
same 24 working themes, as do `--theme` and `config.json`.

The derivation names no colour. Surfaces read `$background`/`$surface`/`$panel`
and the ramp away from the background, which keeps a τ sidebar matching the
Footer painted from `$panel`. The six-step text ramp is the theme's own foreground
at six opacities. `ansi-dark` and `ansi-light` generate `transparent` for every
surface, so both reuse τ's own `_ANSI_PALETTE` rather than a worse copy of it.

`Parley.watch_theme` replaces `action_set_theme`'s write, so a theme picked from
Textual's list persists like one picked from τ's. `--theme` still never reaches
the file.

## Five tools did their file I/O on the loop that paints the screen

The agent loop runs as an async Textual worker on the app's **own** event loop,
with no thread of its own. Anything synchronous inside a tool freezes painting and
input directly, for as long as it runs. `grep` and `find` did a synchronous
`os.walk`; `read`, `write` and `edit` did synchronous file I/O. `bash` was already
correct and is untouched.

All five now do their work in a worker thread. `write`'s atomic sequence moves as
**one** unit: splitting mkstemp / write / `os.replace` across an `await` would let
an abort land between them and leave a `.tmp_write_` file behind with the target
untouched.

Two things turned up while moving `grep` and `find`:

**Neither could be aborted.** Both declared a `signal` parameter and never read
it, so `Esc` during a grep over a large tree did nothing until the walk finished.
Both now poll it — `grep` once per file, `find` once per directory, which is the
unit that costs time in each. The poll happens in the worker thread, which is safe
because `AbortSignal` guards its state with a `threading.Lock`.

**`grep` read every file in the tree twice.** The binary-file check opened each
file, read it whole into memory and threw the result away (`_ = f.read()`) purely
to see whether it decoded — then `_search_file` opened and read it again. The
decode error that pre-pass watched for is the same one `_search_file` already
catches, so the pre-pass is gone. `_search_file` now returns `None` for a file it
could not read and `[]` for one with no matches, which is what keeps
`files_searched` counting the same set it counted before.

## A model repeating one failing call now stops, and says why

0.9.4 made `max_turns` default to `None`. That was right — 50 was a number nobody
could see, raise, or be told about — but it removed a bound that was doing real
work by accident. A model calling the same failing tool forever used to stop when
turn 50 arrived. Then nothing stopped it.

The new check is deliberately narrow, because a wider check is a wider way to be
wrong. It fires only when the **whole batch** repeats — same tool names, same
arguments — **and** every result in every one of those turns is an error. A model
that reads a missing file, writes it, then reads it again has a different batch in
between and never trips it. A model alternating between two failing calls does not
trip it either: that is a claim about progress this check does not make.

The signature is JSON with sorted keys over sorted `(name, arguments)` pairs, so
argument key order and call order do not matter — neither changes what the tools
do. The provider's per-turn `tool_call_id` is deliberately **not** in it: a fresh
id every turn would make every batch unique and the check would never fire.

**The default limit is 3, not 2, and an extension is the reason.** A `tool_result`
hook that reacts to repeated failures — `examples/reminders`'
"root-cause-after-2-failures" — can only fire *on* the second failure, and exists
to change what the model sees before the third attempt. A limit of 2 ends the run
in the same turn the guidance was appended, so the guidance is written and never
read. 3 leaves exactly one turn for an intervention to have an effect.

pi has no equivalent to copy: it has no turn bound at all. This is τ-original.

### `agent_end` now says why the run ended

The same change closes the sibling debt that a stated ceiling was reached
**silently**. `agent_end` carries `end_reason`:

| value | meaning |
|---|---|
| `done` | the model had nothing more to say |
| `terminate` | a `terminate`-ing tool ended the turn |
| `aborted` | the caller aborted |
| `max_turns` | the configured ceiling was reached — the answer is TRUNCATED |
| `repeat_tool_calls` | the repeat check fired |
| `error` | the run raised |

Before it, a run cut off by `max_turns` emitted the same event as one where the
model was finished, so a caller could not tell a truncated answer from a complete
one. It is projected onto `WireEvent` as well — an RPC host needs this as much as
the TUI does.

## An agent asked to extend τ now has a reference

pi backs its "ask it how to extend Pi" startup hint with roughly 12,000 lines of
prose shipped inside the npm package. τ had the inverse shape: 8,359 lines of
test-locked example code and about 1,650 lines of prose answering "why is it like
this" rather than "how do I add one". Five surfaces had no page at all — the
system-prompt placeholder system's only documentation was a docstring.

This release ships the half a machine can produce: a reference generated from the
source, plus the gate that stops it going stale.

| | |
|---|---|
| `tau_llm/docs.py` | `@agent_facing`, a typed no-op marker |
| `tau_agent_core/docs_build.py` | collection, coverage, rendering (pure) |
| `scripts/build_agent_docs.py` | writes `docs/library/reference/` |
| `scripts/check_docs_coverage.py` | the gate |
| `docs/AGENT-DOCS.md` | the design and the measured baseline |

The build reads the source **statically**, with griffe and
`allow_inspection=False`. It imports nothing it documents. That is load-bearing
rather than tidy: the Anthropic and Google providers import their SDKs lazily so
the suite runs without them, and `tau_coding_agent` pulls in Textual. A runtime
decorator registry — the obvious design — would need every one of those modules
imported to find its markers.

Measured at this release: 143 markers, 760 objects, 318 complete (41.8%), 0 drift.
The gate is **not** in the pre-commit hook yet, because a hook that always fails is
a hook people switch off. `docs/AGENT-DOCS.md` §7 records what remains.

## Documentation

`ROADMAP.md` was re-audited against code rather than commit messages. It had
drifted 51 commits and contradicted itself: its own 2026-08-21 header said Session
UX Phases B and C shipped while its "Open work" list called both unbuilt. Six arcs
moved to "Shipped", the 32 flags that exist in `cli.py` are now enumerated so the
two open flag entries cite a list rather than an absence, and a new section
summarizes the debts carried out of the 0.9.4 cycle.

`docs/BLOCKING-PERSISTENCE.md` is new — see "Known open" below.

---

## Upgrade notes

**If you relied on τ's temperature.** τ no longer sends `0.7`. It sends no
temperature at all unless one is set, and the endpoint applies its own default.
This changes sampling for every provider. To keep the old behaviour, set it
explicitly: `models.<name>.temperature` in `~/.tau/config.json`,
`Model(temperature=...)`, or `AgentLoopConfig(temperature=...)`. The field is
`float | None` where the config field used to be `float`.

**If your run can legitimately repeat a failing tool batch.** It now ends after
three such turns with `end_reason="repeat_tool_calls"`.
`AgentLoopConfig.repeat_tool_call_limit` accepts `None` to disable the check, or
any `int >= 2` to change it. **This knob is reachable from `AgentLoop` only** — it is not plumbed through
`AgentSession`, `create_agent_session`, the CLI, or `~/.tau/config.json` the way
`max_turns` is. If you need it on those paths, say so; it is the first thing on
the 0.9.6 list.

**If you consume `agent_end`.** It carries a new `end_reason` field, typed
`AgentEndReason | None`. Additive — nothing was removed or renamed — and it
appears on `WireEvent` too, so RPC hosts see it without a schema change on their
side. A run that previously looked finished may now be visibly truncated; that is
the point.

**If you implement `AgentTool`.** No change. The five built-in tools moved their
I/O to worker threads internally; the `execute` contract is unchanged, and `grep`
and `find` now honour the `signal` they always accepted.

## Known open

**Persistence still blocks the UI thread.** `AgentSession._persist_loop_messages`
is a plain synchronous method at turn end. With the JMFTS store, a ten-tool turn
issues roughly 21 blocking HTTP round-trips on the thread that draws the screen,
and it gets worse as turns get longer. It can be moved off the loop in about six
lines, and that was **rejected**: `SessionLog` is a synchronous `Protocol` with
fourteen append methods, four in-repo implementors and a published contract suite,
so calling it from a worker thread would make thread-safety a new requirement of
every implementor — including ones outside this tree — with nothing in the
protocol, the contract suite, or a version bump saying so.

The honest fix is an async `SessionLog` protocol, which is a breaking change to a
published contract and belongs in its own release with the release note written
first. `docs/BLOCKING-PERSISTENCE.md` records the three options and what each
costs.

With a local JSONL store the same code path is a handful of buffered file writes
and the freeze is not visible.

## Verification

Suite: 4970 passed, 144 skipped, 6 deselected, 0 failed. `mypy` clean across all
five `src` trees. `ruff check` and `ruff format --check` clean. Docs coverage
318/760, 0 drift. Python 3.11, 3.12, 3.13 and 3.14 measured in clean
`python:<v>-bookworm` containers before the tag, per `docs/RELEASING.md` §2.
