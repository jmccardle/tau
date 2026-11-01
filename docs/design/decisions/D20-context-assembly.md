# D20: The system prompt is assembled once, when a session is created

Status: accepted (2026-11-01), every Open question as recommended; proposed 2026-10-31; built
2026-11-01 (differences from this text, all in the code's favour: the module is `tau/context.py`;
`SystemPrompt.sources` is a tuple of `PromptSource(origin, size)`; the shared start's `system`
callable is now optional and defaults to this assembly with the prompt flags, so `--append-system-prompt`
and `-nc` reach it from `args`; `-nc` is accepted with `--fork` as well as with a continue, since
nothing is read for either; a `Config` node holding `system_prompt` was already refused by the
existing "only `model` and `extensions`" rule, so only a test was added; the worktree rule reads
git's `.git` file and `commondir` itself, with no `git` process, and a git file it cannot read is a
`ContextFileError` too; `tau context` asks to trust an untrusted project on a terminal, as a start
does; the built-in base text is `python/tau/prompts/base.md`, from `BASE_SYSTEM_PROMPT` in
`tau-agent-core/src/tau_agent_core/sdk.py` at `v0.15.0`, with its `{{model}}` sentence, `{{tools}}`
and `{{project_context}}` removed and "preferrable" spelled "preferable"). Phase 7, "Context
assembly" (`docs/plan.md`). Amends D13
(one new config key). Restates `model.md` Q5 and I1 without changing them. Depends on D18
(session catalog), D19 (the pinned set) and D21 (the wire) for the interfaces named under
Open. Points marked "(review, 2026-10-31)" were decided in review; they stay proposals until
the owner accepts the ADR.

## Context

τ 0.15 built a system prompt from a built-in base text, the user's flags, and instruction
files found around the working directory. 0.16 has none of this. `Session.create(config,
system, now)` takes a string, and every caller writes its own (`examples/chat.py`:
`SYSTEM = "You are a terse assistant..."`). `docs/from-0.15.md` lists it as a regression.

The plan leaves one question to this ADR: is assembly a startup step of the `tau` program,
or a standard extension? A second question decides the first. Is the prompt recorded in the
tree, so a session replays and exports the same? Or is it rebuilt for every sample, so an
edit to `AGENTS.md` takes effect at once?

**Facts this decision relies on, checked on 2026-10-31:**

- The prompt already lives in the tree. `Session::create` writes the root `Config`, then a
  `System` node holding the text, and opens the first cursor there (`tree.rs:240`; Q5). The
  node's author is `Core`.
- I1: "A sample sends exactly `walk(at)` with `config_at(at)`." Q25: a `pre_sample` hook
  changes the request only by moving the cursor, or by changing the tools it offers. Nothing
  outside the tree reaches the model.
- Every provider makes the `System` nodes at the start of the walk into the instruction.
  Anthropic and Gemini join them with blank lines (`_walk.py` `instruction`). OpenAI sends
  one `system` message for each. A `System` node after the first message becomes a user turn,
  `[system note: <text>]` (D15 D11). So an extension that writes a `System` node before the
  first message adds to the instruction. One written later adds a system note. Both are
  possible today, with no new interface.
- An empty `System` text: Anthropic and Gemini send no instruction. OpenAI sent an empty
  `system` message (`openai.py:96`) until this ADR was built; it now sends none.
- Config: D13's layers are user, then project, then CLI flags, and the nearest wins. The top
  level of a config file accepts `models`, `default_model` and `extensions`
  (`_config_layers.py` `_TOP_KEYS`). The project is the nearest owned `.tau/` from the cwd
  upward, stopping below `$HOME`. An untrusted `.tau/` stops `tau` from starting, or asks a
  live terminal first (`_startup.load_config`, `_trust.find_project`).
- Tools are the driver's. They are handed to each sample and never recorded (roadmap step 7).
- 0.15's behaviour (`v0.15.0`: `tau-agent-core/src/tau_agent_core/sdk.py` 1570-2122,
  `tau-coding-agent/src/tau_coding_agent/cli.py` 305-326, `headless.py` 1088) is in "Changes
  from 0.15", after the decision.

## Decision

### 1. The prompt is a node, written when the session is created

- The assembled text is the `System` node that `Session.create` already writes. Nothing
  changes in core, the store or the bindings. There is no new body kind, no new hook point
  and no new record.
- **It is not rebuilt for each sample.** That would put something outside the tree into
  every request, which breaks I1. A session would then replay, export and branch differently
  depending on the files on disk that day. Recording the text keeps the prompt-cache prefix
  stable. It also makes the log say what the model was told.
- **Consequence: editing `AGENTS.md` does not change a running session or a continued one.**
  To bring an edit in, write a `System` node (a system note) or start a new session. That is
  a command or an extension's job (Open 5). It is not core's, and it is not the startup
  step's.

### 2. A startup step of the program, built as a public function

Assembly is a plain function in the `tau` package. Whatever creates a session calls it and
passes its text to `Session.create`: the `tau` program, `tau serve` for a remote client (D21),
an example, or a user's own frontend.

```python
loaded = tau.load_config(cwd, environ, ask=..., cli={"system_prompt": text})  # D13, as now
prompt = tau.system_prompt(cwd, loaded, append=("...",), context_files=True)
prompt.text  # the System node's text
prompt.sources  # each part, in order: where it came from ("base", "config", "flag",
# or a file's path) and its size in bytes
session = tau.Session.create(config, prompt.text, now, directory)
```

It is not an extension, for three reasons:

- **Timing.** The node is written by `Session.create`, before any cursor steps. So no hook
  has run yet. An extension could only add its part at the first `pre_step`. To do that it
  would need a "session created" hook, which Q23 keeps out until something needs one. Or it
  would have to check the walk on every step to see whether its part is already there.
- **Fail early.** An unreadable `AGENTS.md` should stop `tau` before a session file exists.
  As a hook failure, it would arrive after the session was created, with the user's first
  line already pending, as a failed step.
- **The flags are the program's.** `--system-prompt` is the CLI layer of a config key
  (§3), and the CLI layers are built before any extension loads (D19).

"Leave it to an extension" still holds, in two ways:

- Core is untouched, and a frontend that wants another prompt passes another string.
- An extension that wants a part of the instruction, such as Phase 8's skills list, writes
  its own `System` node before the first message, authored by itself. That needs no new
  interface (Facts).

Unix composition covers the rest. For example, `tau -nc --system-prompt "$(cat my.md)"`.

### 3. What goes in, in order

The text is the parts below, each joined by one blank line. A part with no text is left out.

1. **Base.** The nearest layer's `system_prompt` wins: `--system-prompt` (the CLI layer),
   then the project's `.tau/config.json`, then the user's `config.json`. With none set, the
   built-in base text is used, a data file in the package (Open 1). `"system_prompt": ""`
   means no base text at all, which is a choice. D13's `null` drops an inherited value.
   **`system_prompt` is set from the config files and the command line only; a `Config` node
   in the tree cannot set it** (review, 2026-10-31), as with D19's `load`: the prompt is
   written once, at creation, not per branch. The base text is literal: no `{{fields}}`.
2. **Appended sections.** Each `--append-system-prompt TEXT`, in the order given. CLI only.
3. **Context files**, unless `--no-context-files` / `-nc`. Each is wrapped in
   `<project_instructions path="/abs/path">`, all inside one `<project_context>` block; the
   path tells the model which directory the instructions are about. In order:
   - one file from `$TAU_HOME`;
   - one file from each directory from `/` down to the cwd (the nearest is read last, so it
     reads as the most specific);
   - `.tau/SYSTEM.md` from the **trusted project** (D13), if it has one.

   For the first two, each directory gives the first of `AGENTS.md`, `CLAUDE.md` that is a
   regular file (symlinks followed). A file reached twice by resolved path is read once. A
   worktree nested in its own main repository hides the main repository's file of the same
   name (0.15's rule, kept: it is how `.claude/worktrees/` is laid out). **`.tau/SYSTEM.md`
   does not follow symlinks** (review, 2026-10-31): if trust pins it (Open 2, recommended
   yes), it is read through trust's reader from the approved bytes, as D19 §4 does for
   `.tau/extensions/`, and a symlink there is refused as trust refuses one for `config.json`.

### 4. Sessions that already exist

The prompt is whatever is in the tree.

| situation | what happens to the prompt |
|---|---|
| continuing or opening a session (D18) | Nothing is read: no files, no config `system_prompt`. `--system-prompt` or `--append-system-prompt` is a usage error: "the session already has its system prompt (node 1); start a new session for a different one". `-nc` is accepted: nothing would have been read anyway. |
| reopening a cursor (D17 `reopen`) | Unchanged: the new cursor's walk passes through the same `System` node. |
| a later `Config` node (a model switch) | Unchanged. |
| continuing from another working directory | Unchanged: a session has one working directory, fixed at creation, and its tools run there (D18 §1), so the prompt stays true. |
| `--fork REF` (D18) | The new session copies the walk from its root, so the `Config` root and the `System` node are copied verbatim (as records; provenance is `forked_from` in `info.json`, D18 §6). The prompt flags are an error with `--fork`, as in 0.15, because the copied walk is not rewritten. A different prompt is a new session, not a fork. |
| a detached root (a cold compaction summary, D3) | Has no `System` node unless the extension that writes it copies the leading ones. That is Phase 8 compaction's to do, and it is stated here so it is not forgotten. |
| export to Markdown (D18) | The `System` node is exported like any other node. |

### 5. Trust, limits, errors

- **`AGENTS.md` and `CLAUDE.md` need no trust.** They are text the model could read with
  `read` in any case. D13's trust protects keys and code: a project config can redirect
  `base_url`, and extensions run code. A prompt file can only ask the model for things. The
  defense against what it asks is permissions (Phase 8), not a gate on reading. Gating them
  would prompt on every cloned repository.
- **`.tau/SYSTEM.md` is read only in a trusted project**, because `tau` does not start in an
  untrusted one. Whether trust pins its content is Open 2; if so, §3's reader applies.
- **No size limit, no truncation.** A file that is too big fails the first sample with the
  provider's error (Q11). `tau context` shows each part's size so the cause is visible. A
  cap would be a guard against a rare case, or would silently cut the instructions.
- **A file that was found but cannot be used stops the start.** This covers permission
  denied, invalid UTF-8, a broken symlink, and an I/O error. `ContextFileError` names the
  path and the cause, and suggests `-nc`. Decoding is strict UTF-8, and a leading BOM is
  dropped. The `tau` program prints the message and exits 1, as for any `ConfigError`, and no
  session is created. "Found" means a regular file (for `AGENTS.md` and `CLAUDE.md`, after
  following symlinks). A directory or FIFO with a context file's name is not a context file,
  and the search goes on, as in 0.15; reading a FIFO would block.
- **The working directory is the new session's** (D18 §1). The shared start has already
  changed the process into it (D18 §5), so assembly's "cwd" and every other consumer's agree;
  it is resolved with symlinks followed, as D13's project search does (review, 2026-10-31).

### 6. `tau context`

`tau context [-nc] [--system-prompt T] [--append-system-prompt T]... [--show]` runs the same
assembly in the cwd without creating a session. It lists each part: where it came from and
its byte count. `--show` prints the text instead. It answers "what will the model be told,
and from where?" before a session exists. After that, the answer is node 1.

## Changes from 0.15

What 0.15 did, and what 0.16 keeps or changes on purpose:

- **Base text.** 0.15's `BASE_SYSTEM_PROMPT` was a "Tau, a coding agent" template with the
  fields `{{model}}`, `{{cwd}}`, `{{project_context}}` and `{{tools}}`; a config
  `system_prompt` or `--system-prompt` replaced it, and an unknown field was an error.
  **Changed: no templates.** `{{model}}` is dropped: the text is recorded, and a later
  `Config` node can switch the model (D5, D13), after which it would be wrong. `{{tools}}` is
  dropped: tools are offered per sample and never recorded, and a `pre_sample` hook can change
  which a sample gets. `{{project_context}}` has no use without the others. `{{cwd}}` becomes
  a sentence in the built-in text, written in at assembly. A replacement base names the
  directory itself, or relies on the model running `pwd`.
- **`--append-system-prompt`** (repeatable, after the base, before the context files):
  **kept**.
- **Context files.** 0.15 took the first of `AGENTS.override.md`, `AGENTS.md`, `AGENTS.MD`,
  `CLAUDE.md`, `CLAUDE.MD` per directory, from `$TAU_HOME`, every ancestor of the cwd and
  `<cwd>/.tau/SYSTEM.md` last, deduplicated through symlinks, wrapped as in §3. **Kept:** the
  order, one file per directory, the dedupe and the worktree rule. **Changed:** the name list
  is `AGENTS.md`, `CLAUDE.md` (Open 3); `SYSTEM.md` comes from the project's `.tau/`, not only
  the cwd's, because `.tau/` means something only as the trusted project directory (D13); and
  it can be pinned (Open 2).
- **`-nc`** skips all context files, `SYSTEM.md` included: **kept**.
- **Errors.** Strict UTF-8, BOM dropped, `ContextFileError` naming the path and `-nc`, a
  directory with a context file's name skipped, no size limit, no trust gate for `AGENTS.md`
  or `CLAUDE.md`: **kept** (§5).
- **When.** Built once, written as the session's first message, and the stored prompt used on
  resume: **kept**. `--system-prompt` with `--continue`, `--session` or `--fork` was an error:
  **kept**. **Changed:** `--append-system-prompt` and `-nc` on a resumed session were silently
  ignored; now the append flag is an error and `-nc` is accepted (§4).
- **Extensions.** A `before_agent_start` hook replaced the prompt for one run, unsaved (the
  codemode extension used it): **dropped**. An extension adds to the instruction with its own
  `System` node (§2).
- **Tests.** 23 in `tests/test_context_files.py` and 6 in a second 0.15 test module are
  rewritten as behavior (Build).

## Amends

- **D13**: a new top-level key, `system_prompt` (a string, or `null`), in the user and
  project config, plus its CLI layer. A `Config` node that sets it is refused (§3). `tau
  config check` reports a wrong type. Its `--show` lists which layer the base text comes from.
- **`model.md`**: Q5's row adds "the text is assembled by `tau.system_prompt` (D20)". I1 is
  unchanged, and this ADR relies on it.

## Files and format impact

None. The log, the scratchpad and `VERSION` are untouched. The prompt was already a node.

## Alternatives rejected

| option | why not |
|---|---|
| Rebuild the prompt at every sample | Breaks I1. A session's request depends on today's files, so replay, export and branching stop being faithful, and prompt caches miss after every edit. |
| A standard extension that writes the prompt | §2. |
| A hook that replaces the prompt for one sample (0.15's `before_agent_start`) | Breaks I1 and Q25: it sends the model something the tree does not hold. |
| One `System` node per part; a `Custom` node recording paths and hashes | `Session.create` takes one string and the path headers already name each file. Nothing reads such a record yet; a refresh command (Open 5) can add one. |
| Keep `{{fields}}` | See "Changes from 0.15": the fields become false once recorded. |
| Stop the upward search at the git root, or at `$HOME` like D13 | Drops `~/Development/AGENTS.md`-style instructions that 0.15 users rely on. Context files are instructions, not settings. |
| Gate `AGENTS.md` on trust | See §5: prompts on every cloned repository and protects nothing `read` doesn't expose. |
| Warn and skip an unreadable file (pi) | A prompt quietly missing its instructions looks exactly like a model that ignored them. |
| Accept a file path for `--system-prompt` when one exists | Guessing between text and path surprises both ways. Use `"$(cat f)"`. |
| Ignore prompt flags on a continued session (0.15 did for append) | The user asked for something that does not happen, and nothing says so. |

## Open (accepted as recommended, 2026-11-01)

1. **The built-in base text.** Recommendation: carry 0.15's base prose minus the model
   sentence and the tools list, as `python/tau/prompts/base.md`, with the cwd sentence filled
   in when assembled. This rests on the owner's relaxation of rule 1 (`CLAUDE.md`, plan Phase
   6): 0.15 presentation (prose, UI) may be carried over selectively, harness code never, and
   the commit names its 0.15 source (review, 2026-10-31). The alternative is a new, shorter
   text written for 0.16. Either way, the owner reviews the words.
2. **Does trust pin `.tau/SYSTEM.md`?** Recommendation: yes, in the same hash set as
   `config.json` and `.env`. It is the project's own tau configuration, in the directory
   that trust approves, and a `git pull` that changes it should show up as a diff. D19 §4
   lists its hash (`system_sha256`) beside `.tau/extensions/`'s, and it is read from the
   approved bytes (§3). Without a pin it is still never read through a symlink.
3. **The name list.** Recommendation: `AGENTS.md`, `CLAUDE.md`. Drop `AGENTS.override.md`
   (Codex's convention, unused here) and the `.MD` spellings. Each is one line to add back.
4. **Continuing from another directory.** Answered by D18 §1, as recommended here: a
   session has one working directory, recorded when it is created, and its tools run there.
   Continuing from somewhere else does not move it. A fork's directory is D18's Open 1.
5. **Bringing an edited `AGENTS.md` into a session.** Recommendation: not in Phase 7. A
   Phase 8 standard command, `/context`, can re-assemble and write the context-file part as a
   `System` node, which reaches the model as a system note. It sends the files again rather
   than a diff, and it is cheaper than a new session.
6. **Config keys for append and `-nc`.** Recommendation: none, as in 0.15. A user who always
   wants them sets `system_prompt` or uses a shell alias. Add them when someone asks.

**Interfaces needed from siblings:**

- **D18**: `--fork` copies from the root, so the `System` node is copied verbatim. New,
  continue and fork are told apart before assembly, so the prompt flags can be refused on
  the latter two. It answers Open 4.
- **D19**: extensions load after the CLI layers. D19 §4 pins `SYSTEM.md` with
  `.tau/extensions/` (Open 2).
- **D21**: no wire call creates a session (D21 §1). `tau serve` takes the prompt flags with
  the other startup flags and assembles in its own process, through D18 §5's shared start.
  A `ContextFileError` is the server's start error, with its message intact (D21 §9).

## Glossary additions

- **system prompt**: the text of the `System` node `Session.create` writes. It is assembled
  once by `tau.system_prompt` and never rebuilt (D20).
- **base text**: the first part of the system prompt. It comes from the nearest
  `system_prompt` layer, or the built-in text.
- **context file**: an `AGENTS.md` or `CLAUDE.md` found in `$TAU_HOME`, the cwd or an
  ancestor, or the trusted project's `.tau/SYSTEM.md` (never through a symlink). It is put in
  the system prompt with its path.
- **`system_prompt`** (config key, D13 layers, files and command line only; `--system-prompt`
  is its CLI layer).
- **`tau.system_prompt(cwd, loaded, *, append, context_files)`** → **`SystemPrompt`**
  (`text`, `sources`).
- **`ContextFileError`** (`ConfigError`): a context file that was found and cannot be read.
- **`tau context`**: the CLI view of an assembly.

## Build (rough lines, tests separate)

| item | lines |
|---|---|
| `tau/context.py`: discovery (ancestors, `$TAU_HOME`, project `SYSTEM.md` from the approved bytes, worktree shadowing, dedupe), strict read, rendering, `SystemPrompt` | ~180 |
| `system_prompt` key: `_TOP_KEYS`, type check, refused in a `Config` node, merged read, `--show` source | ~30 |
| CLI: the three flags on whatever creates a session (D18's command), refusal on continue and fork; `tau context` | ~60 |
| `prompts/base.md` and package data | ~40 text |
| OpenAI provider: an empty leading `System` sends no message, as Anthropic and Gemini do | ~3 |
| `examples/chat.py` uses `tau.system_prompt` | ~5 |
| tests: the 0.15 tests (Changes from 0.15) rewritten as behavior through the public API and `tau.cli.main` (ancestors, the nearest read last, one file per directory, symlink dedupe, worktree shadowing, `SYSTEM.md` from the project and refused through a symlink, unreadable and non-UTF-8 files, BOM, `-nc`, flag refusals, `system_prompt` in a `Config` node refused, the recorded prompt surviving a reload) | ~300 |
