# 0.9.3 — the first hour after `pip install`

Status: **plan, agreed 2026-08-21.** Nothing here is built yet.

0.9.2 put τ on PyPI. Strangers can now install it, and the scope below follows
the path each of them walks: install, run in a project, come back tomorrow and
resume. Every claim was checked against the code on 2026-08-21, not against a
status doc.

## 1. Project context files never load

### What is true today

`_build_system_prompt` (`sdk.py:671`) reads `AGENTS.md` and `.tau/SYSTEM.md`
from **cwd only**. It works: dropping an `AGENTS.md` into a directory and
calling it directly puts the file's text into the prompt.

It is reachable, at `sdk.py:847`:

```python
sys_prompt = system_prompt or _build_system_prompt(cwd, tool_objs)
```

But `tau_default_config.json` ships `system_prompt` set to
`"You are a helpful assistant. Be concise and clear."`, `TauBackend` passes it
through (`backends.py:1001,1068`), and a non-empty string is truthy. So on a
default install the loader is **never reached** and no project context is read.

ROADMAP.md calls this an orphaned loader on an unreachable path. That is the
wrong diagnosis: the code is live and shadowed by a default value. The fix is
precedence, not plumbing.

Two further consequences of the same line. The two base prompts compete, and
the weaker one always wins — `_build_system_prompt` opens with "You are τ… You
can help with coding, file editing, and system commands", which the config's
"helpful assistant" string replaces wholesale. And a user who sets
`system_prompt` in their own config silently turns off context files, which
nothing tells them.

### What pi does

pi is the source of truth, and it answers every open question here.
`loadProjectContextFiles` (`resource-loader.ts:119`, pi `5cd93f688`):

1. Load a context file from the **agent dir** first (τ's `~/.tau`).
2. Then walk from cwd **up to the filesystem root**, taking at most one file
   per directory, ordered root-most first so the nearest file is read last.
3. Deduplicate by resolved path.
4. Per directory, first match wins among `AGENTS.override.md`, `AGENTS.md`,
   `AGENTS.MD`, `CLAUDE.md`, `CLAUDE.MD` (`resource-loader.ts:72`).
5. **Shadowing** (`findShadowedContextFile`, `resource-loader.ts:101`): when
   cwd is inside a git worktree nested under its own main repo, the worktree's
   context file suppresses the main repo's same-named file, so the ancestor
   walk does not load both. It deliberately does nothing for an ordinary repo,
   a sibling worktree, a bare layout, or a submodule.

Context files load **by default**. `--no-context-files` / `-nc`
(`args.ts:185`, help at `args.ts:302`) turns discovery off. They compose *with*
the system prompt; they are not an alternative to it.

> pi moves fast. These citations were re-checked at `5cd93f688` and had already
> drifted from a reading taken hours earlier — `loadProjectContextFiles` moved
> 77→119, and `AGENTS.override.md` plus the whole shadowing rule were new. Cite
> pi with a sha, and re-verify before porting.

Rule 5 is not academic for τ: this repo keeps worktrees in `.claude/worktrees/`
*inside* the repo, which is exactly the layout it exists to handle.

### The gap

| | τ today | pi |
|---|---|---|
| Discovery | cwd only | agent dir + every ancestor to `/` |
| Names | `AGENTS.md`, `.tau/SYSTEM.md` | `AGENTS.override.md`, `AGENTS.md`/`.MD`, `CLAUDE.md`/`.MD` |
| `CLAUDE.md` | not supported | supported |
| Worktree shadowing | n/a | yes, and this repo has the layout |
| Composes with system prompt | no — either/or | yes |
| Opt out | none | `--no-context-files` / `-nc` |
| Loads on a default install | **no** | yes |

### Proposed

Port pi's discovery, keeping `.tau/SYSTEM.md` as a τ-only addition since it is
already documented. Add `--no-context-files`/`-nc`. Make context files compose
with the system prompt rather than being displaced by it.

**Open decision — the shipped default.** Composition alone leaves "You are a
helpful assistant. Be concise and clear." in front of every prompt on every
install. Three ways out, and this needs a human call:

1. Drop `system_prompt` from the default config and let
   `_build_system_prompt`'s own base text stand. Smallest change; the base
   text already names τ and its job.
2. Keep a config key but ship it empty, so the default is the base prompt and
   the key remains the documented override.
3. Replace the default string with a real coding-agent prompt.

## 2. `--resume` cannot succeed

`cli.py:675` raises for `args.resume` before the `--print` branch is reached,
so the flag errors in **both** modes. Its help says "TUI only". Its error says
it "isn't available headlessly" and to use the sidebar. Both cannot be true.

Nothing outside `cli.py` reads `args.resume`; there is no picker.

This is Fail-Early working — it refuses rather than no-ops — but a flag that
can never succeed should not ship. It stops being a contradiction the moment
§3 lands, which is why it is sequenced first as a two-line message fix and
resolved properly by the picker.

## 3. Session UX Phase B + C

The design is settled in `docs/SESSION-UX-REDESIGN.md` — the picker in §6, the
command unification in §7, the sidebar default in §8, the phasing in §9.
Phase A (storage) shipped 2026-06-23. Only the build of B and C is missing, so
this is not a design task.

- **Phase B** — the Textual-native picker modal (§6), reading `SessionInfo`
  (§5.7) and the listing/scoping rules (§5.8). `--resume` becomes real.
- **Phase C** — one action reachable from three surfaces (§7), and the sidebar
  closed by default (§8, `parley.tcss:9-13` today has no startup override).

`SessionTreeModal` is a branch-tree browser and is **not** this. Do not
extend it into a session picker.

## 4. LLM backend flexibility

Added to scope 2026-08-21, prompted by a field bug report against the AskSage
OpenAI-compatible gateway. Two studies of pi's provider layer informed this
section; pi citations are at `5cd93f688`, τ claims were re-verified directly.

The headline surprise: **pi does not solve most of this**. It is streaming-only,
it has no turn bound at all, and it does not validate tool-call names either. On
this axis τ is not porting pi — it is going past it, in three places
deliberately.

### 4.1 τ cannot talk to a non-streaming backend — and pi cannot either

`openai.py:1098` hardcodes `"stream": True`. `"stream"` is in
`_RESERVED_BODY_KEYS` (`openai.py:105`), so a model's extra-body cannot override
it. A backend that does not implement SSE is unusable.

pi is no help here. Every chat-capable pi API hardcodes the same thing —
`openai-completions.ts:766`, `anthropic-messages.ts:1006`,
`openai-responses.ts:293`, `mistral-conversations.ts:511`. Its `complete()` and
`completeSimple()` (`compat.ts:266-298`) look like non-streaming entry points but
just await the same `stream: true` request down to its final message. The only
`stream: false` in pi's whole AI package is an image endpoint
(`openrouter-images.ts:160`).

**So this is a deliberate divergence, not a port.** CLAUDE.md requires those to
be intentional and recorded; this is the record. Proposed: a per-model
`stream: bool` (default true), a matching CLI flag, and a non-streaming path that
adapts one completion response into the same
`TextDeltaEvent`/`ToolCallDeltaEvent`/`DoneEvent` sequence, so nothing above the
provider learns which mode ran.

It is also a *workaround surface*: §4.2's defect is in streamed chunks, and the
non-streaming response may carry the field the stream omits.

### 4.2 The AskSage report — what is and is not τ's

> **Follow-up, 2026-08-23 — built.** A second report against the same gateway
> covered the buffered responses this section did not see, and proposed a
> client-side shim. The shim's diagnosis was right and its packaging was not: a
> global, silent, permanently-installed normalizer that translated any
> `function`-less tool call carrying a top-level `name`, and defaulted a call
> with no argument payload to empty. What shipped instead is
> `compat.tool_call_schema` (`tau_llm/compat.py`) — operator-stated per model,
> never detected, raising rather than defaulting — plus the enriched diagnosis in
> `_build_final_message` that tells the two failure modes apart, plus a real τ
> bug the report surfaced: `delta.get("tool_calls", [])` treated a
> present-and-null key as a value and raised `TypeError: 'NoneType' object is not
> iterable`. That `TypeError`, not the Azure preamble frame the report blamed,
> was the crash; the preamble frame was already handled by the `if not choices:
> continue` guard §4.3 records. Measurements, shapes and the rejected shim are in
> `docs/RELEASE-NOTES-0.9.4.md`; the tests are sections (5) and (6) of
> `tau-llm/tests/test_backend_hardening.py`.

The root cause is upstream and correctly diagnosed. The gateway's `gpt-5*` and
`gpt-o3*` deployments never populate `function.name` on any tool-call SSE chunk,
while `gpt-4.1*`, `gpt-5.1-gov` and `gpt-o4-mini` on the *same* gateway do —
proven with byte-identical payloads differing only in `model`. τ transcribes what
arrives. That transcription is correct and must stay correct.

Three consequences are τ's, and each is a Fail-Early violation.

**1. τ builds and executes a tool call with no name.** The empty name reaches
`_execute_tool`, `self._tools.get("")` misses, and the loop reports
`Unknown tool: ` — an invalid value proceeding deeper into the pipeline.

The sharp part: **τ already contains the correct guard, and it is dead code.**
`_convert_openai_choice_to_message` (`openai.py:811`) does exactly the right
thing at `openai.py:849`:

```python
if tc_id and tc_name:
    tool_calls.append(ToolCall(id=tc_id, name=tc_name, arguments=args_dict))
```

Nothing in production calls that method. Its only callers are
`tests/test_openai_provider.py` — verified by grep across the package. The live
path is `_build_final_message` (`openai.py:952-1011`), and there the asymmetry is
visible in adjacent lines: it **raises** `ValueError` when an argument buffer does
not decode to a dict (`openai.py:980-983`), then appends
`ToolCall(id=tc.id, name=tc.name, arguments=args_dict)` (`openai.py:986`) with no
check on `name` at all.

The fix is to extend the raising finalize path τ already has to the one field it
skipped. pi does not do this — `ensureToolCallBlock`
(`openai-completions.ts:450-507`) lets a block finalize with `name: ""`, and
`agent-loop.ts:607-613` then reports `Tool  not found`. τ is not regressing from
pi; τ has machinery pi lacks and did not point it at this field.

**2. The loop cannot notice it is looping.** `agent_loop.py:211`/`376` bound only
on `max_turns` (`agent_loop_types.py:75`, default 50) — the exact number in the
report. Note pi has **no turn bound whatsoever**: `agent-loop.ts:155-275` exits
only on error, on no-more-tool-calls, or via a host-supplied
`shouldStopAfterTurn`. τ's cap is already a τ-original safeguard; it is just a
blunt one, and does not notice that turns 2–50 are the identical failure. Compare
the current call's `(name, arguments)` against the preceding failed result and
stop on an exact repeat. This is new design, with nothing in pi to copy.

**3. Errors can be empty.** `openai.py:1361` builds
`ErrorEvent(message=f"Streaming error: {str(e)}")` and `agent_loop.py:852`
re-raises it bare. `httpx.ReadTimeout`, `ConnectError` and `RemoteProtocolError`
all stringify to `""` when raised with no message, so a dropped connection
surfaces as `RuntimeError: Streaming error: ` with nothing after the colon.

pi solves this properly and τ has no equivalent: `normalizeProviderError` /
`formatProviderError` (`utils/error-body.ts:38-135`) probe SDK-specific shapes for
status and body, truncate the body to 4000 chars, and compose
`"<status>: <body>"` even when the message itself is thin. At minimum τ should
carry the exception type and never emit a content-free message.

### 4.3 Robustness gaps where pi is genuinely ahead

- **No retry or backoff anywhere in τ.** pi has two layers:
  `utils/provider-retry.ts` (retryable on 408/409/429/5xx or an
  `x-should-retry` header; `retry-after-ms` → `retry-after` → exponential
  `0.5·2^n` capped at 8s with jitter; a hard `maxRetryDelayMs` that fails fast
  when the server asks for longer), and `utils/retry.ts` at the assistant-turn
  level with an explicit non-retryable quota/billing exclusion. Worth knowing
  before porting: pi's second layer is wired only into compaction, not the main
  turn loop, and **neither layer retries a mid-stream drop** — that is terminal
  in pi too.
- **Timeouts are hardcoded with no override.** `openai.py:472` fixes
  `httpx.Timeout(300.0, connect=10.0)` at client construction, and there is no
  `Model` field or per-call option — confirmed, `grep` over `types.py` and
  `client.py` finds nothing. pi threads a per-request `timeoutMs`
  (`types.ts:159-163`) through every call. Since the reported failure is a
  connection-lifetime question, an operator currently cannot tune it without
  editing provider source.
- **A non-dict SSE frame crashes instead of being skipped.** `openai.py:1212`
  does `json.loads` then `chunk.get(...)` at `openai.py:1220` with no
  `isinstance(chunk, dict)` guard. pi skips these explicitly
  (`openai-completions.ts:510`). A proxy emitting `data: []` as a keepalive would
  raise `AttributeError` into the broad handler, and back into the empty-message
  problem above.

τ's SSE reader is otherwise equal to pi's on the edge cases that matter, and
already handles the AskSage usage chunk correctly: it reads `usage` **before**
the `if not choices: continue` guard (`openai.py:1220-1231`), the same ordering
pi uses. One deliberate difference to keep in view — pi **throws** when a stream
ends with no `finish_reason` (`openai-completions.ts:651-653`); τ falls through
silently with `final_stop_reason=None`.

### 4.4 Multi-vendor breadth is cheaper than the provider count suggests

pi ships 39 providers, but roughly 25 are thin configurations over one
OpenAI-compatible client — `providers/groq.ts` is 15 lines. Only 7 wire protocols
are genuinely distinct clients (Anthropic messages, Google generateContent,
Vertex, Bedrock Converse, Mistral conversations, OpenAI Responses, and pi's own
`pi-messages`), and those are 1300–1400 lines each.

τ's blocker is not the client count. It is that **τ can only ever construct one
provider class**: `_get_or_create_provider` (`client.py:78-98`) returns
`OpenAICompletionsProvider` unconditionally, ignoring `provider_name`, which it
uses only as a cache key. And the types forbid a second vendor outright —
`types.py:115` pins `AssistantMessage.provider` to `Literal["openai"]`, so even
Groq or DeepSeek cannot be labelled honestly in τ's own output.

Cheapest useful order:

1. Widen `Model.api` / `AssistantMessage.api` / `.provider` from `Literal[...]`
   to `str` (`types.py:114-115,143`). Trivial, and it currently blocks everything.
2. Dispatch in `client.py` on `model.api`/`model.provider` instead of hardcoding
   the class. With (1), that reaches pi's largest bucket for near-zero new code,
   because τ's OpenAI-completions path already takes an arbitrary base URL and key.
3. A real `Provider` interface (`id`, `auth`, `get_models()`, `stream_chat`) plus
   a factory, so a thin vendor is a short file rather than a hand-rolled class.
4. Port pi's `detectCompat()` (`openai-completions.ts:1534-1629`) as a per-model
   `compat` field auto-detected from base URL. τ already has the destination
   knobs (`thinking_level_map`, `extra_body`, `grammar_dialect`) but makes the
   operator set each by hand; pi infers `max_tokens` vs `max_completion_tokens`,
   `store` support and the reasoning-field shape from the vendor.
5. Pluggable auth, then a model resolver, then distinct clients — last, and only
   on real demand for a specific non-OpenAI-shaped vendor.

### 4.5 Where the model's facts come from — **built 2026-08-21**

Steps 1 and 2 shipped in `6e1dfbe`. Step 4 shipped as `tau_llm.compat`, and it
is **two fields, not pi's twenty-six** (three since 2026-08-23 — see the §4.2
follow-up; `tool_call_schema` is the one field here that is never detected,
because it is the one that could hide a fault rather than surface one). That is the finding, not a shortcut: most
of pi's compat is already said by a τ `Model` field, and saying it twice invites
the two to disagree. `supportsReasoningEffort` is `Model.reasoning`;
`thinkingFormat` and `supportsThinkingTokenBudget` are `thinking_level_map`,
whose fragment form names the field *and* its value instead of selecting from an
enum τ would have to keep current; the routing and chat-template dicts are
`extra_body`. Seven more guard requests τ never makes. What was left is
`max_tokens_field` and `supports_usage_in_streaming` — the two keys `extra_body`
cannot reach, because one is reserved and the other is written after the spreads.

**Detection is inverted from pi's, deliberately.** pi lists the servers wanting
`max_tokens` and gives everyone else `max_completion_tokens`, so an unrecognised
endpoint — for τ, usually a local llama.cpp — gets a spelling it rejects. τ names
only `api.openai.com` and `openai.azure.com` and leaves everyone else on the
classic key. τ also does not match on the provider NAME as pi does, because
`build_model_from_config` defaults an unnamed backend to `provider="openai"`, so
in τ that string usually means "unstated". Matching it would have flipped every
local config that never named a backend.

**Step 4's other half was the catalog.** The complaint was that an operator sets
each knob by hand; the deeper problem was that two of them could not be set at
all — `build_model_from_config` hardcoded `context_window=128000` and
`max_tokens=4096` for every model in existence, with no config key reaching
either. Both keys are now read, and `python -m tau_llm.catalog` fills them from
[models.dev](https://models.dev/api.json) (MIT) along with `reasoning` and
`thinking_level_map`, converted through a port of pi's
`getEffortThinkingLevelMap`.

Nothing is vendored. `tau_llm.providers.__init__` already states why τ ships no
vendor list — every URL and environment-variable name in one is a claim τ would
have to keep true — and a snapshotted catalog is the same claim at larger scale.
The tool fetches when asked and prints a config entry to stdout for the operator
to inspect. models.dev carries no base URL (a provider record is `id`, `name`,
`doc`, `npm`, `env`), so `--base-url` is required and never guessed.

Both projects are now credited in the root README and in `tau-llm/README.md`.

Still open from §4.4: step 5 (pluggable auth, a model resolver, distinct
clients).

**Step 3 shipped after this section was written** — `tau_llm/providers/base.py`
has the `Provider` ABC (one abstract method, `stream_chat`, plus `aclose`) and
two registries, and `client.py:199` dispatches through `get_api_factory`. A
vendor is a `ProviderSpec` record rather than a class, so an OpenAI-compatible
backend is six fields. What step 5 still owes is a client for a wire protocol
that is not OpenAI-shaped.

**Do not "fix" toward pi parity:** τ's constrained-decoding machinery
(`providers/openai.py:202-303`) and its turn-scoped `reasoning_replay`
(`types.py:183-198`) have no pi equivalent and are ahead of it. Generalizing the
provider layer must preserve them.

## 5. Release mechanics

Small, and they gate the release itself.

- **The OIDC path has never run.** TestPyPI 0.9.2 was a hand token upload, so
  `publish.yml`'s publish job is unproven and 0.9.3's tag push would be its
  first execution. Prove it with a TestPyPI dispatch early in the cycle, once
  normal publishers exist there — not on release day.
- **`publish.yml`'s header comment is wrong.** It says one identity covers all
  four projects. True after bootstrap, false for a project that does not exist
  yet, which is what cost 0.9.2 an afternoon. Correct it in the file that
  governs releases.
- **`dist/` has no `.gitignore` rule.** A local `python -m build --outdir dist`
  leaves an untracked directory. `tau-*/build/` is already ignored.
- **The test matrix is 3.11 only.** `publish.yml`'s own comment says 3.13 and
  3.14 are "being measured separately". Measure them, or drop the claim.
- **0.9.2 has no GitHub tag or release.** Publishing the draft creates the tag,
  which fires the pipeline, which fails at `publish-pypi` because 0.9.2 is
  already on PyPI and `skip-existing` is deliberately off. Decide: accept one
  red run to record the release, or leave 0.9.2 without a GitHub release and
  make 0.9.3 the first clean one.

## 5b. Remove branch lanes

**Done, 2026-08-21.** The contract suite was salvaged rather than cut (one test
inverted, one re-argued); `subtree_text` became descendant-bounded rather than
lane-bounded. `docs/LANE-REMOVAL.md` §8 records what was built, the
measurements, and the release note for downstream `SessionLog` implementors.

Added 2026-08-21 from a design review of `subtree_text`. Full write-up in
`docs/LANE-REMOVAL.md`; the short version:

The `branchOf` tag is stamped by three stores and read by four callers, and
**three of the four use it to answer a question it does not answer**. They want
"does this entry belong to the conversation being looked at?" — ancestry from
the cursor — and get write provenance instead. So a three-way fork and three
sub-agents from one point, structurally identical trees, are treated oppositely:
`Session.messages` returns all three fork branches as one conversation,
`message_count` counts abandoned ones, and a session title can come from one.

The tag's only real job is repairing `resolve_cursor`'s "last entry wins"
inference under a second concurrent writer. **That requirement is dropped on the
record:** τ is an interactive agent, not a service that must survive `pkill`
consistently, and resuming on the wrong leaf is a visible annoyance the tree
browser fixes. So `resolve_cursor` returns to pi's rule and the tag goes.

Moving the three consumers to ancestry is **not** optional — without it,
sub-agent turns start showing up in `messages` and in the picker.

`BranchView` stays; its isolation is structural (the leaf→root walk), not the
tag's doing. The TUI's multi-lane *rendering* also stays — that is a separate
`lane` concept, a live routing key, and it accounts for 165 of the ~215 test
references to the word.

~20 source edits, ~50 test references. The real ripple is
`tau_agent_core.testing`'s published contract suite; the owner has accepted that
API cost if nothing in it can be salvaged.

Sequence it **before** resolving `SessionManager._extract_branch_messages`
(§6) — the dead twin lacks the containment its live counterpart has, and the
live one's containment is keyed on the wrong field.

## 6. Debts, unscheduled

Not in scope unless they get pulled in deliberately.

- `docs/TECTUM-NO-TOOLS-MIGRATION.md` — six sites, still the Tectum owner's
  call, still live on the dev box.
- 50 mypy findings under mypy 2.3.1; the gate currently pins an older mypy
  (2.1.0, confirmed 2026-08-21).
- ~~`create_agent_session()` takes no `no_tools`, so the tri-state is reachable
  only through the CLI.~~ **Done, 2026-08-21.** The factory takes
  `no_tools: Literal["all", "builtin"] | None`, and does two things beyond
  forwarding it.

  It **empties the built-ins itself**. `AgentSession._build_turn_tools` reads only
  `no_tools == "all"` and relies on a documented invariant — both policies "arrive
  here with `self._tools == []`" — which until now was established solely at the
  coding-agent's argv boundary (`headless.resolve_no_tools` +
  `backends.resolve_tool_names`). So `"builtin"` had no behaviour inside
  `tau_agent_core` at all; an SDK caller who passed it got a display label. It has
  one now.

  It **refuses `tools=` and `no_tools=` together**, because that call asks for
  opposite things — a named built-in, and no built-ins — and neither parameter
  outranks the other at a call site. `tools=None` and `tools=[]` stay legal:
  neither asks for a built-in, and `"all"` is still meaningful on top of them since
  it also withholds extension-registered tools. Value validation stays in
  `AgentSession` — one copy of the literal list.

  Rejected: accepting two booleans and resolving them here. `resolve_no_tools`'s
  own docstring records why — flags that only have meaning against each other
  become the same flag once every consumer re-derives the interaction.

  This does **not** fix Tectum (`docs/TECTUM-NO-TOOLS-MIGRATION.md`), which reaches
  τ through the CLI and is unaffected either way.
- ~~51 `.tau/sessions/*.jsonl` files tracked~~ — **done 2026-08-21.**
  Untracked with `--cached`; `docs/RELEASING.md` §4 needs no change, because it
  rebuilds the public tree from tracked files only.
- ~~`origin/feat/extensions-e6` and six `worktree-agent-*` refs~~ — **done
  2026-08-21**, and both halves of this line were wrong. `feat/extensions-e6`
  no longer existed on `origin` at all, and there were **sixteen**
  `worktree-agent-*` branches, not six; all sixteen were merged and are
  deleted. `origin/rpc/tier-b` survives, is also fully merged, and is the one
  remaining deletable remote ref — left alone because deleting it is a push to
  the shared server.
- ~~Five spec docs still say "Status: design. No code written."~~ — **done
  2026-08-21**, and four of the five had already been fixed before this entry
  was read. The one with real work left was `docs/CLI-PLAN.md` §3, which needed
  a full flag-by-flag resync rather than the four-flag fix ROADMAP.md described:
  only seven flags remain unbuilt. ROADMAP.md's doc-hygiene section now records
  what closed each entry.

## 7. Sequencing

1. Release mechanics (§5) — prove OIDC, fix the comment, `.gitignore`, matrix.
2. **Backend hardening (§4.2, §4.3)** — the empty-name raise, a non-empty error
   message, the `isinstance(chunk, dict)` guard, a configurable timeout. These
   are small, independent, and each one turns a silent 50-turn burn or a
   content-free `RuntimeError` into a sentence. Do them before anything larger.
3. Context files (§1) — port pi's discovery, add `-nc`, settle the default.
4. `--resume` message fix (§2) — two lines, stops the contradiction now.
5. Non-streaming mode (§4.1) — τ-original; no pi design to follow.
6. Multi-vendor steps 1–2 (§4.4) — widen the literals, dispatch by provider.
7. Phase B (§3) — the picker. `--resume` becomes real.
8. Phase C (§3) — command unification, sidebar default.

Release mechanics go first because a release nobody can cut is worth less than a
feature nobody has yet. Backend hardening goes second because it is the only
item with a user waiting on it, and because items 5 and 6 both touch the same
provider file — hardening it first means they land on a sound base rather than
widening a defect across vendors.

§1, §3 and the §4 work are otherwise independent.

## 8. Non-goals

Trust gate (Tier 8), Tier 9 output surfaces, Tier 10, and Tier 11 M4/M5 stay
out of 0.9.3. They are ordered in ROADMAP.md's "Suggested order" and none of
them is on the newcomer's path.
