# E5.5 — live procedures (manual, per-demo)

> **Reference: EXTENSIONS-E5-WIRING.md §6 (E5.5 / S37).** The hand-run companion to
> the automated floor (S36, `tau-coding-agent/tests/test_e5_integration_floor.py`).
> S36 proves the wiring in a headless subprocess + a Textual `Pilot`; this file is the
> **manual** checklist a human runs against a real interactive `tau` to *see* each
> demo's durable node land on the session tree with their own eyes.

Every procedure below is one **exact command**, the keystrokes/prompts to drive it,
and the **expected durable-node observation** — what appears on the active path, in
**all three** of its guises, because under the durable-hook invariant (§1) they are
one artifact:

- **transcript** — the scrolling chat log in the TUI;
- **tree** — the tree browser (`/tree`, or `Ctrl+G`), which shows the active path's
  nodes; and
- **on disk** — the persisted session JSONL under
  `~/.tau/sessions/<cwd-hash>/<timestamp>-<id>.jsonl` (`persisted == rendered == sent`).

A "durable node" that shows up in the transcript but not the tree/disk (or vice
versa) is exactly the divergence E5 forbids — if you see that, it is a bug, not a
pass.

> **Prerequisite — a working model.** These are *live* runs against a real
> OpenAI-compatible endpoint. Point `~/.tau/config.json` `default_model` at a server
> you can reach (the repo default `local-llm` at a local vLLM/Ollama is ideal — local
> servers stream argument fragments aggressively and exercise the whole loop). No
> `-p`: every procedure here is the **interactive TUI**, because the observation is a
> node you inspect in the tree browser.

> Each demo loads through the public `-e` file-path surface: `tau -e <file>` →
> `sdk._load_one_extension` → `getattr(module, "register")`. Each example now exposes
> a module-level `register` alias (e.g. `register = gatekeeper_extension`), so the
> commands below load the *real* example file, not a wrapper.

---

## 1. Gatekeeper — a blocked `is_error` node

**Command:**

```bash
tau -e examples/22_gatekeeper.py
```

**Setup (the gate is fail-CLOSED — an undeclared scope denies every write):**

1. In the run's `cwd`, create `.tau/scope.txt` with a single allowed prefix, e.g.
   `src/` on its own line. Create `src/`. Do **not** grant anything outside it.

**Drive:**

2. At the prompt, ask the agent to write a file **outside** the declared scope — e.g.
   *"Create the file /etc/tau_probe.txt with the text hello."* (any path that does not
   resolve under `src/`).
3. Let the model emit the `write` tool call.

**Expected durable node:** the `write` call never executes; the `tool_call` veto
converts it into a **`toolResult` node with `is_error: true`** whose text is exactly
the gatekeeper's reason — `Denied: write to /etc/tau_probe.txt is outside the allowed
scope (no matching prefix in .tau/scope.txt).` In the **transcript** it renders as a
blocked/error tool box; in the **tree** (`/tree`) it is a real node on the active
path between the assistant's tool call and the model's next turn; on **disk** it is a
`message` entry with `role: "toolResult"`, `is_error: true`. The model reacts to it
like a non-zero exit — no filesystem write happened.

---

## 2. Reminders — a `<system-reminder>` appended to the failing `tool_result`

**Command:**

```bash
tau -e examples/21_reminders.py
```

**Drive (the `root-cause-after-2-failures` rule needs two same-tool failures):**

1. On the **first** turn, watch for the standing discipline preamble: the
   `before_agent_start` hook seeds it once (see §5's reload note).
2. Ask the agent to do something that makes the **same tool fail twice in a row** —
   e.g. *"Run `cat /nope/missing_a.txt`, and if that fails run `cat
   /nope/missing_b.txt`."* Two consecutive `bash` errors trip the rule.

**Expected durable node:** on the **second** failing result, the reminder bank
**edits that `tool_result` node in place**, appending a
`<system-reminder>The same tool has failed twice in a row. Stop repeating the
identical action; investigate the root cause before the next attempt.</system-reminder>`
text block *beneath the tool's own error output* (append, never replace). It is a
durable edit: the **transcript** shows the reminder under the failing tool box, the
**tree** node for that `toolResult` carries the extra text block, and the **on-disk**
`toolResult` entry's `content` ends with the reminder block. The next LLM call sees
the edited node exactly as the interface shows it — there is no separate per-call
injection.

---

## 3. Budget — a durable warning node, then abort

**Command:**

```bash
tau -e examples/24_budget.py
```

> The deployable default is **token mode** with a `DEFAULT_MAX_TOKENS = 500_000`
> ceiling (no `cost` block → no fabricated dollar figure). To trip it in a short live
> run, either lower the ceiling by loading a `make_budget_extension(max_tokens=…)`
> variant, or drive a long enough session that cumulative tokens cross it.

**Drive:**

1. Give the agent a multi-step task that keeps calling tools (so `message_end` usage
   accumulates turn over turn).
2. Watch the running total climb past the ceiling.

**Expected durable node:** on the first `tool_result` after the completion whose
usage crossed the ceiling, the guard **appends a one-shot warning block** —
`<system-reminder>Budget exceeded: … tokens used (ceiling …). The run is being
stopped now; wrap up rather than starting new work.</system-reminder>` — to that
result's `content`, then calls `ctx.abort()`. The loop's per-turn abort check breaks
before the next turn, so this warning is the **last** durable node of the run. It is
visible in the **transcript** (warning under the final tool box), present in the
**tree** on the active path, and persisted **on disk** in that `toolResult` entry's
`content`. The abort halts before another LLM round-trip, so the warning records *why
the run ended* in the transcript rather than being re-sent to the model.

---

## 4. Delegate — spawn a child

**Command:**

```bash
tau -e examples/20_delegate.py
```

**Drive:**

1. Ask the agent to delegate a small, self-contained subtask — e.g. *"Use the
   delegate tool to have a subagent summarize examples/README in one sentence."*
2. The `delegate` tool spawns a separate `tau -p --mode json --no-session
   --no-extensions` child process (its own isolated context window), captures the
   child's rolled-up output, and returns it as the tool result.

**Expected durable node:** a `delegate` **`toolCall` node** followed by its
**`toolResult` node** carrying the child's final output (and a `details` payload with
the child's `usage` / `stop_reason` / `model`). Both are real nodes on the active
path: the **transcript** shows the delegate tool box with the child's summary, the
**tree** shows the call + result pair, and **on disk** they persist as the
`assistant` tool-call entry and its `toolResult` entry. The child ran with
`--no-extensions` (recursion-safe: it cannot re-load `delegate`), so nothing from the
child leaks onto the parent path except that one durable `toolResult`.

---

## 5. Reload check — the invariant's proof (no second history)

**Commands:**

```bash
tau -e examples/21_reminders.py
tau --resume
```

**Drive:**

1. First command: run the reminders demo far enough to persist at least one durable
   injected node — the `before_agent_start` **discipline-preamble `customMessage`
   node** is seeded on turn one, so a single prompt is enough. Note the transcript,
   then quit (`Ctrl+C`).
2. Second command: `tau --resume` and pick that same session from the interactive
   picker (or `tau --session <timestamp-stem>` to name it directly).
3. Open `/tree` and compare against what you saw before quitting.

**Expected durable observation:** the reloaded session is **byte-identical** to what
was persisted — the injected preamble `customMessage` node is present **exactly once**
(no forked "model-saw copy" + "disk copy"), in the same active-path position, and the
transcript is unchanged. The load does **not** rewrite the file, and the model context
reconstructed from the reloaded path is identical to the pre-quit one (the edit is
baked into the node, not recomputed on send). On the wire the durable `custom` node
still remaps `custom → user` (pi `messages.ts`), so the model never sees a `custom`
role — but the reader/tree do. A *second* injected copy, a missing node, or a rewritten
file on reload would each be the divergence the invariant forbids; seeing exactly one
node, unchanged, is the pass.

---

## What "pass" means (all five)

For each procedure the durable node must appear in **transcript AND tree AND on-disk
JSONL** — because they are the same artifact — and, for §5, must **survive reload
byte-identically as exactly one node**. Any guise showing the node while another does
not is a failure of the durable-hook invariant, not a cosmetic glitch.
