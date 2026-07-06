export const meta = {
  name: 'extensions-e0-e4',
  description: 'Execute the E0-E4 extensions plan (S1-S23) as a sequential implement->review->fix->commit chain',
  phases: [
    { title: 'E0 loader+flags' },
    { title: 'E1 connect API +cost' },
    { title: 'E-json' },
    { title: 'delegate' },
    { title: 'E2 mutating hooks' },
    { title: 'E2 demos' },
    { title: 'E3-ctx' },
    { title: 'capstone' },
  ],
}

const REPO = '/home/john/Development/agent-harness-py'
const BRANCH = 'feat/extensions-e0-e4'

const RULES = `Rules:
- pi (~/Development/pi) is the SOURCE OF TRUTH for API shape. Read the corresponding pi file before diverging. Read docs/EXTENSIONS-IMPLEMENTATION.md (both the named phase section AND the §8 step bullet) and CLAUDE.md before coding.
- Fail-Early (global rule): NO fallbacks, dummy data, silent no-ops, or placeholder logic. Prefer raising. If a subproblem tempts a workaround, implement the correct pi-faithful behavior instead.
- Implement EXACTLY this one step — do not pull work forward from later steps or refactor unrelated code.
- Add/extend the tests named in the step's Verify clause. Tests must actually exercise the new behavior through the real loop (use the existing fake_llm / fake provider test harness where the step calls for a loop test). No fake/tautological tests.
- This is one link in a STRICT SEQUENTIAL CHAIN on branch ${BRANCH}; later steps depend on this commit being correct and green.`

const GATE = `Gate (run from repo root ${REPO}; the in-repo venv is REQUIRED):
  source venv/bin/activate
  venv/bin/ruff check tau-ai/src tau-agent-core/src tau-coding-agent/src
  venv/bin/ruff format tau-ai/src tau-agent-core/src tau-coding-agent/src   # auto-format; then --check in the hook passes
  venv/bin/mypy tau-ai/src tau-agent-core/src tau-coding-agent/src
  python -m pytest -q
All four MUST pass before you commit. The pre-commit hook (core.hooksPath=.githooks) re-runs ruff+mypy on commit and BLOCKS a red gate automatically. pytest is NOT in the hook — you MUST run it yourself and it must be fully green.`

const COMMIT = `Commit on the current branch (${BRANCH}). Do NOT push. Do NOT create new branches. Exactly ONE commit for this step.
Subject: concise conventional-commit prefixed \`feat(ext):\` (or \`docs:\` for doc-only steps) naming the step — e.g. \`feat(ext): S3 bind ExtensionAPI to live session/bus/registry\`.
End the commit message body with these two trailer lines EXACTLY:
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QrzgbtnSK5b4VzFuoVC9K8`

const IMPL_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    committed: { type: 'boolean' },
    sha: { type: 'string' },
    summary: { type: 'string' },
    filesChanged: { type: 'array', items: { type: 'string' } },
    testsAdded: { type: 'array', items: { type: 'string' } },
    gateGreen: { type: 'boolean' },
    blocked: { type: 'boolean' },
    blockReason: { type: 'string' },
    notes: { type: 'string' },
  },
  required: ['committed', 'summary', 'gateGreen', 'blocked'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    verdict: { type: 'string', enum: ['approved', 'changes_requested'] },
    mustFix: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          summary: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'number' },
        },
        required: ['summary'],
      },
    },
    notes: { type: 'string' },
  },
  required: ['verdict', 'mustFix'],
}

const FIX_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    fixed: { type: 'boolean' },
    sha: { type: 'string' },
    summary: { type: 'string' },
    gateGreen: { type: 'boolean' },
    stillBroken: { type: 'boolean' },
    notes: { type: 'string' },
  },
  required: ['fixed', 'gateGreen', 'stillBroken'],
}

const STEPS = [
  { id: 'S1', phase: 'E0 loader+flags', title: 'one loader',
    brief: `Delete tau_agent_core/extensions/loader.py (dead) and its extensions/__init__.py re-export; rewrite sdk._load_extensions as THE single loader: verb register(api), file-path importlib (spec_from_file_location), await async factories, discovery = global ~/.tau/extensions + explicit paths only (NO project-local dir, NO importlib.metadata entry_points), dedup by resolved path (first-wins). Return LoadExtensionsResult{extensions, errors:[{path,error}]} (port pi types.ts:1590). Explicit -e load failure RAISES; a discovered failure → errors[] + stderr, continue. Files: sdk.py, extensions/__init__.py, canonical extensions/loader.py (or fold into sdk.py). Verify: tests — discovered vs explicit load, -ne suppresses discovery but keeps -e, broken-discovered collected while others load, broken-explicit raises, register(api) invoked (not returned un-called), async register awaited.` },
  { id: 'S2', phase: 'E0 loader+flags', title: 'CLI flags',
    brief: `Add -e/--extension (append, path), -ne/--no-extensions (disables DISCOVERY only — explicit -e still load), -xt/--exclude-tools (csv denylist), -nbt/--no-builtin-tools, --no-session, --append-system-prompt to cli.py build_parser + CLIArgs; thread each into headless.py and the run config. -nbt degenerates to --no-tools for now (document the degeneracy). Map pi args.ts:104-153. Files: cli.py, headless.py. Verify: each flag parses + reaches the headless run config; --no-extensions keeps explicit -e.` },
  { id: 'S3', phase: 'E1 connect API +cost', title: 'bind the API (KEYSTONE)',
    brief: `_make_extension_api() (agent_session.py:533-540) must construct ONE ExtensionAPI(session=self, event_bus=self._events, registry=<session-owned>) bound to the REAL refs (self._events is the loop bus, agent_session.py:109) — today it hands a bare ExtensionAPI() with a fresh orphan EventBus, so every handler subscribes to a dead bus. Drop the legacy backward-compat mirrors (_handlers/_active_tools/_commands/_session_name, extension_types.py:226-231) and update the tests that leaned on them. api.on(event, handler) must now subscribe to the live bus. Files: agent_session.py, extension_types.py. Verify: api.on('tool_execution_end', …) receives a live event with real payload over a fake_llm turn.` },
  { id: 'S4', phase: 'E1 connect API +cost', title: 'registered tools reach the loop',
    brief: `The AgentLoop is rebuilt per prompt()/continue_conversation (agent_session.py:249-255,333-339) with tools=self._tools only; the registry is never read. Resolve the registry's active extension tools into AgentTool instances and merge them into the per-turn tools list (so runtime registration is live-next-turn for free). Port register_tool (extension_types.py:254, currently raw dict) to pi's ToolDefinition shape (types.ts:435-482): parameters as a JSON-schema DICT (do NOT require Pydantic/TypeBox), name/description/parameters/execute(tool_call_id, params, signal, on_update, ctx). Files: agent_session.py, extensions/registry.py, extension_types.py. Verify: a registered fake tool becomes callable and executes through the loop; a second register_tool mid-session is live the next turn.` },
  { id: 'S5', phase: 'E1 connect API +cost', title: 'return-collecting dispatcher',
    brief: `Build a SEPARATE ExtensionRunner-equivalent alongside the notify EventBus (pi keeps them separate, types.ts:1347; the bus stays fire-and-forget for the 10 AgentEvents). It dispatches the MUTATING hook events as their own typed events (a parallel typed dispatch, NOT an extension of the AgentEvent Literal), iterating extensions in load order and handlers in registration order, awaiting each and threading the return value forward. Expose has_handlers(event) for the no-extension fast path (pi agent-session.ts:405-411). E5 lands the dispatcher + wiring only; the four hook CALL-SITES land in E2. Files: new extensions/runner.py (or events.py sibling). Verify: unit — collect + chain + short-circuit; no-handler fast path returns without work.` },
  { id: 'S6', phase: 'E1 connect API +cost', title: 'real usage + de-fictionalize injection',
    brief: `Replace get_context_usage() stub ({"total_tokens":0}, extension_types.py:161-163) with pi's ContextUsage shape {tokens: int|None, context_window: int, percent: float|None} (types.ts:281-287) reading estimate_context_tokens (already computed for auto-compact, agent_session.py:514). Fix send_user_message: default off "steer" → "followUp"; validate deliver_as ∈ {followUp,nextTurn} while leaving the param a plain string (extensible). Remove the silent hasattr no-op (extension_types.py:305-313) → RAISE if the queue isn't present yet (the real queue lands in E3-ctx). Files: extension_types.py. Verify: real non-zero usage over a seeded session; bad deliver_as raises; send_user_message raises (not silent) until the queue exists.` },
  { id: 'S7', phase: 'E1 connect API +cost', title: 'cost at the emit boundary',
    brief: `Optional per-model cost:{input,output,cache_read,cache_write} (USD per 1M) block in config.json, carried by model_config (headless.py:46-66). Port pi calculateCost (models.ts:39-48) collapsed: sum(price[k]/1e6 * usage[k]). Compute cost_usd at the EMIT BOUNDARY — backends.py (usage_totals, :520-531) + headless.py done (:279) where model_config is in scope — final done/agent_end ONLY; the frozen Usage is untouched. EMIT cost_usd ONLY WHEN the cost block is present — an absent block yields tokens-only, NEVER a fabricated $0 (a real free model cost:{…:0} and unknown cost MUST read differently — the subtle Fail-Early trap). cache_write is inert today (comment it). Files: backends.py, headless.py, config docs. Verify: cost present with config, absent without; free cost:{…:0} ≠ absent.` },
  { id: 'S8', phase: 'E-json', title: 'pi-faithful --mode json',
    brief: `Add a tau_event → pi AgentSessionEvent serializer sourced from the AgentEvent bus: "type" discriminator (NOT "kind"), per-message message_end carrying usage/model/stop_reason, session header line emitted FIRST. Behind --mode json (gate the old kind schema behind a flag or replace — decide at build time; a demo adapter reads whichever ships). This is pi Tier-9 json, pulled forward so delegate children emit real limit/failure signals. Files: backends.py, headless.py. Verify: json stream carries message_end with usage/model/stop_reason; header line first.` },
  { id: 'S9', phase: 'delegate', title: 'examples/20_delegate.py',
    brief: `delegate tool spawning: tau -p --mode json --no-session --no-extensions --model … --tools … --append-system-prompt <tmp> "Task: …" children (pi subagent/index.ts:288-324). Modes: single / parallel-N (≤8, 4-concurrent, 50 KB/task cap) / chain ({previous}). Per-child limits max_usd/max_seconds/max_turns/stuck-detection + stop_reason taxonomy reading the E-json child signals; roll usage into details. PARALLEL mode forces a read-only --tools allowlist via a HARD CODE-GUARD (write-tool classification a small constant list) — Fail-Early. Files: examples/20_delegate.py + smoke test. Verify: smoke test spawning tau -p against the fake provider — single + parallel + a forced-read-only assertion.` },
  { id: 'S10', phase: 'E2 mutating hooks', title: 'thread the dispatcher',
    brief: `AgentSession injects the E5 hook-dispatcher callable (returning results) into AgentLoop.__init__ (the loop today holds only fire-and-forget emit, agent_loop.py:90). All hook sites gate on has_handlers for the zero-extension fast path. This step just wires the dispatcher in; the four call-sites land in S11-S14. Files: agent_session.py, agent_loop.py. Verify: dispatcher reachable from the loop; has_handlers fast path when no extensions.` },
  { id: 'S11', phase: 'E2 mutating hooks', title: 'tool_call hook',
    brief: `Wire tool_call at _prepare_tool_call, at the PreparedToolCall return (agent_loop.py:804). Event {type, tool_call_id, tool_name, input} → {block?, reason?}. Mutate input IN PLACE to patch args. First block:true short-circuits → convert to BlockedCall (reuse the existing dataclass, agent_loop.py:51-59; error result text = reason). EXCEPTION = fail-CLOSED block (pi agent-session.ts:419-424). NO re-validation after mutation (pi parity). Verify: veto blocks execution + error text = reason; in-place arg patch reaches the tool; a throwing tool_call handler blocks.` },
  { id: 'S12', phase: 'E2 mutating hooks', title: 'tool_result hook',
    brief: `Wire tool_result at _apply_after_hooks (agent_loop.py:888, called :637 seq / :694 par; currently an explicit no-op). Event {…, content, is_error, details} → {content?, details?, is_error?} partial patch. Clone once, field-patch the shared event across handlers (later handler sees earlier's patch); whole-value replace, no deep merge; none set → pass through. Errors here swallow-and-continue but MUST surface via an emit_error-equivalent (never silently drop, pi runner.ts:754-763). Verify: patch replaces content/is_error; chained across two handlers.` },
  { id: 'S13', phase: 'E2 mutating hooks', title: 'before_agent_start hook',
    brief: `Fire before_agent_start in AgentSession.prompt() just before loop.run() (agent_session.py:~247). Event {prompt, images?, system_prompt} → {system_prompt?, message?}. system_prompt CHAINS (last wins, live to later handlers); message(s) ACCUMULATE, injected as custom messages. Verify: two handlers chain the system_prompt + accumulate two messages.` },
  { id: 'S14', phase: 'E2 mutating hooks', title: 'context hook',
    brief: `Fire context in _stream_response before building the context dict (agent_loop.py:376), on a DEEP COPY (structuredClone-equivalent). Event {type:'context', messages} → {messages?} replaces. Fires BEFORE EVERY LLM call (not per-turn). This is the <system-reminder> seam. Verify: an injected <system-reminder> is visible on the wire payload sent to the provider.` },
  { id: 'S15', phase: 'E2 demos', title: 'examples/22_gatekeeper.py',
    brief: `tool_call veto demo: deny writes outside .tau/scope.txt prefixes; deny reads/bash touching tests_heldout/. The enforcement that makes the agent-callable mutation tools safe. Files: examples/22_gatekeeper.py + smoke test. Verify: an out-of-scope write is blocked; a held-out read is blocked.` },
  { id: 'S16', phase: 'E2 demos', title: 'examples/21_reminders.py',
    brief: `The four-rule bank from pi_planning_implementing_evaluating.md §2 (tests-readonly / root-cause-after-2-failures / scope-guard / no-new-deps): track state on tool_call/tool_result, inject <system-reminder> via the context hook with per-rule cooldowns 3/4/2/1. Read event.input.path (τ controls the field — drop pi's args??input dual-read). Files: examples/21_reminders.py + smoke test. Verify: each rule fires once then cools down.` },
  { id: 'S17', phase: 'E2 demos', title: 'examples/24_budget.py',
    brief: `Accumulate message_end/turn_end usage, computing a running $ from tokens × the model's config cost block; past a USD (or token) threshold inject a warning via the context hook then ctx.abort() (abort already exists on the API). Lands after E2 because its warn-then-abort needs the context hook. Files: examples/24_budget.py + smoke test. Verify: aborts past threshold; warning injected before abort.` },
  { id: 'S18', phase: 'E3-ctx', title: 'bind the live Session (D3 refactor)',
    brief: `The TUI AgentSession is today constructed with a throwaway InMemorySessionLog (the E3-1d scratch log) while the live Session is owned by the TUI. Retire that split: (1) construct the TUI's AgentSession with the LIVE Session as its SessionLog (drop the scratch InMemorySessionLog, backends.py E3-1d wiring); (2) make AgentSession the SOLE persister — remove app.py's own append_message writes (app.py:1145,1239), resolving the E3-1d double-write tension; (3) the TUI transcript render becomes a VIEW over ConversationTree — self.messages rebuilt from session.context (the Session.context seam already landed) at structural points only (turn-end / compact / navigate), matching pi rebuildChatFromMessages (interactive-mode.ts). Incremental streaming render unchanged. Files: backends.py, app.py, agent_session.py. Verify: one write path (no double-persist); TUI resume/render behavior unchanged; existing suite green. Sequence this FIRST within E3-ctx.` },
  { id: 'S19', phase: 'E3-ctx', title: 'ctx op surface',
    brief: `Give ExtensionContext a handle to the AgentSession, then add these methods delegating to the LANDED code: compact(custom_instructions=None) → AgentSession.compact (agent_session.py:368; + deferred variant in S20); entries() → SessionLog.entries()/ConversationTree pass-through; summarize_branch(from_entry, custom_instructions=None) → module summarize_branch (session_manager.py:705, already raise-based) via subtree_text → append_branch_summary à la backends.py navigate_tree (:159-206); navigate(target_id, summarize=False, …) → append_navigate/append_branch_summary (session_store.py:396-434); fork(entry_id=None) as ONE op with an in-place-vs-export mode (in-place = navigate+append; new-file = Session.fork, session_store.py:347 → path). Expose on the base handler context so agent tools can call them (the E2 gatekeeper veto is the safety). Files: extension_types.py (ExtensionContext), agent_session.py. Verify: each ctx op mutates the one session log + re-renders context_for.` },
  { id: 'S20', phase: 'E3-ctx', title: 'deferral + injection queue',
    brief: `Deferral (decision 3): a tool requesting compact/fork records intent and returns a normal result; drain it at the TAIL of prompt() — the SAME site as _maybe_auto_compact() (agent_session.py:301), i.e. end-of-prompt(), NOT per-inner-turn. No loop reentrancy. send_user_message queue: add the _queue_message the API already calls (extension_types.py:305) — followUp drains at end-of-prompt (re-enters within the same call), nextTurn queues for the next prompt(). Both share the end-of-prompt drain with deferred compact/fork. deliver_as stays extensible. Files: agent_session.py, extension_types.py. Verify: deferred compact_now applies exactly once at end-of-prompt (not mid-turn); followUp vs nextTurn land at the defined points.` },
  { id: 'S21', phase: 'E3-ctx', title: 'seam-3 onto the bus',
    brief: `subscribe_session_events emits raw dicts {type, session, **extra} (session_store.py:47-70) with no consumer. Route them onto the extension bus via a SEPARATE string channel (EventBus.emit_channel) — NOT by extending the AgentEvent Literal (which has no session members, events.py:46-70). Gives session_start/session_before_fork/session_before_compact/session_shutdown their first consumer. Files: events.py, session_store.py / agent_session.py wiring. Verify: an api.on('session_before_compact', …) handler fires.` },
  { id: 'S22', phase: 'capstone', title: 'examples/23_context_surgeon.py',
    brief: `Agent tools composing E3-ctx + E2 safety: compact_now (turn-deferred), summarize_history(from_entry), fork_session(entry_id) → returns the forked path + optionally spawns a delegate. Composes demos 20_delegate + 22_gatekeeper; lands last. Files: examples/23_context_surgeon.py + headless smoke test. Verify: headless smoke test of each tool.` },
  { id: 'S23', phase: 'capstone', title: 'walkthrough doc',
    brief: `A docs/ composed-run walkthrough: gatekeeper + reminders + budget wrapping a delegate-driven plan→implement→evaluate loop. NO code — prose that ties the demos together. Commit prefix docs:. Verify: doc renders; references the shipped example filenames accurately.` },
]

const results = []
let halted = null

for (const step of STEPS) {
  phase(step.phase)
  log(`${step.id} — ${step.title}: implementing`)

  const impl = await agent(
    `You are implementing step ${step.id} of the τ extensions E0–E4 chain.\n\n` +
    `STEP ${step.id} — ${step.title}\n${step.brief}\n\n` +
    `${RULES}\n\n${GATE}\n\n${COMMIT}\n\n` +
    `Work now: read the relevant docs + pi source, implement the step, add the Verify tests, get the full gate + pytest green, and commit. Return the structured result (set committed=true only if you actually committed a green commit). If you are genuinely blocked by a spec contradiction you cannot resolve pi-faithfully, set blocked=true with a precise blockReason and do NOT commit — do not invent a workaround.`,
    { label: `impl:${step.id}`, phase: step.phase, schema: IMPL_SCHEMA }
  )

  if (!impl || !impl.committed) {
    results.push({ id: step.id, title: step.title, status: 'FAILED', stage: 'implement',
      summary: impl ? impl.summary : 'implement agent died', blocked: impl ? impl.blocked : true,
      blockReason: impl ? impl.blockReason : 'agent returned null', notes: impl ? impl.notes : '' })
    halted = step.id
    break
  }

  log(`${step.id}: reviewing commit ${impl.sha || 'HEAD'}`)
  const review = await agent(
    `Adversarially review the latest commit implementing step ${step.id} of the τ extensions chain.\n\n` +
    `STEP ${step.id} — ${step.title}\n${step.brief}\n\n` +
    `Inspect the commit: run \`git show HEAD\` and read the changed files in full. Judge ONLY this step against its intent + Verify clause. Check for: correctness, pi-parity (pi is source of truth, ~/Development/pi), Fail-Early violations (fallbacks / dummy data / silent no-ops — flag any), missing or fake tests (a test that does not actually exercise the new path), and whether the gate + pytest genuinely cover the Verify. Do NOT demand scope beyond this step. Do NOT manufacture nits.\n\n` +
    `Return verdict 'approved' if the step is correctly and completely implemented and tested (empty mustFix — the expected outcome for good work). Return 'changes_requested' ONLY for concrete defects, each in mustFix with file (+ line where possible).`,
    { label: `review:${step.id}`, phase: step.phase, schema: REVIEW_SCHEMA }
  )

  const mustFix = review && review.mustFix ? review.mustFix : []
  const needsFix = review && (review.verdict === 'changes_requested' || mustFix.length > 0)

  let fix = null
  if (needsFix) {
    log(`${step.id}: ${mustFix.length} must-fix → fixing`)
    fix = await agent(
      `The reviewer requested changes on step ${step.id} (${step.title}) of the τ extensions chain. Apply the genuine fixes, keep it pi-faithful and Fail-Early, re-run the full gate + pytest, and AMEND into the existing step commit (\`git add -A && git commit --amend --no-edit\`; keep the two trailer lines). Do NOT push. Do NOT add a second unrelated commit.\n\n` +
      `MUST-FIX ITEMS (JSON):\n${JSON.stringify(mustFix, null, 2)}\n\n` +
      `Reviewer notes: ${review && review.notes ? review.notes : '(none)'}\n\n${GATE}\n\n` +
      `Return the structured result. If a listed item is a FALSE POSITIVE (the code is already correct/pi-faithful), do not change code for it — explain in notes and set it aside; only fix genuine defects. If you cannot make the gate green, set stillBroken=true.`,
      { label: `fix:${step.id}`, phase: step.phase, schema: FIX_SCHEMA }
    )

    if (fix && fix.stillBroken) {
      results.push({ id: step.id, title: step.title, status: 'FAILED', stage: 'fix',
        summary: fix.summary || impl.summary, mustFixCount: mustFix.length,
        notes: (fix.notes || '') + ' | review: ' + (review.notes || '') })
      halted = step.id
      break
    }
  }

  results.push({
    id: step.id, title: step.title, status: 'DONE',
    sha: (fix && fix.sha) || impl.sha,
    summary: impl.summary,
    filesChanged: impl.filesChanged || [],
    testsAdded: impl.testsAdded || [],
    review: review ? review.verdict : 'no-review',
    mustFixCount: mustFix.length,
    fixApplied: !!fix,
    notes: [impl.notes, review && review.notes, fix && fix.notes].filter(Boolean).join(' | '),
  })
  log(`${step.id}: DONE (review=${review ? review.verdict : '?'}, fixes=${mustFix.length})`)
}

return {
  branch: BRANCH,
  completed: results.filter(r => r.status === 'DONE').map(r => r.id),
  haltedAt: halted,
  steps: results,
}
