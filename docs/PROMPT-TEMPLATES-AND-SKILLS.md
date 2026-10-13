# Prompt templates and skills

Built 2026-10-12 for the user and explicit scopes; the project scope for skills
is not read until the trust gate exists. `/review the parser` now expands
`review.md` into the prompt, and `~/.tau/skills/<name>/SKILL.md` is listed in
the system prompt and runs as `/skill:<name>`. Both reach every head, because
the expansion happens inside `AgentSession.submit()`.

This closes ROADMAP Tier 10's templates and skills legs, with one scope held
back (§3). The themes leg shipped on 2026-08-24 without this loader.

## 1. What was missing

Re-grepped 2026-10-12 over the five `src` trees: no `PromptTemplate` type, no
`$ARGUMENTS` handling, no `SKILL.md` reader, and no shared loader for Markdown
resources. `Submission.expand_commands` already said in a comment that it
governed "templates/skills expanded", and nothing read it for that.

Building this turned up a defect that was already in `tau -p` (read from
`headless.py` before this change; no test exercised it). Print mode appended
the typed `prompt_text` to its session file itself, then persisted only the
non-user messages the turn returned. So:

- an `input` hook that rewrote the prompt had its rewrite sent to the model
  and the original saved to disk;
- a queued `nextTurn` user message was sent and never saved, because it is a
  user message;
- a `before_agent_start` message positioned before the user message was
  sent before it and saved after it.

These three are read from the code, not reproduced; the template case is
reproduced by `test_print_mode_persists_the_expanded_prompt`.

A template is an input transform, so it would have hit the first fault on
every use. §7 fixes all three.

## 2. The shared part: `tau_agent_core/resources.py`

Templates and skills are both Markdown with optional frontmatter. They are found
in the same scopes, resolved by the same rule, and fail the same way. The
module holds those parts, and the two kinds never import each other.

### 2.1 Three scopes

| Scope | Templates | Skills |
|---|---|---|
| `explicit` | `--prompt-template PATH`, the model entry's and config.json's `prompt_templates` lists | `--skill PATH`, `skills` lists |
| `user` | `~/.tau/prompts/*.md` | `~/.tau/skills/<name>/SKILL.md` |
| `project` | `<cwd>/.tau/prompts/*.md` | `<cwd>/.tau/skills/<name>/SKILL.md`, **not read** (§3) |

`--no-prompt-templates` and `--no-skills` turn off the user and project scopes.
Explicit paths still load, which is the rule `-e` already follows under
`--no-extensions`. A missing user or project directory is not an error. A
missing explicit path raises, because the operator named it.

Only `<cwd>` is checked for a project directory, not its ancestors, which
matches `.tau/SYSTEM.md`. Context files walk the ancestors (`load_project_context_files`).
The two rules differ on purpose. A `CLAUDE.md` is meant to apply to everything
below it. A `.tau/` directory configures the run started in it.

### 2.2 First wins, strongest scope first

Explicit beats user, and user beats project. Within a directory, files load in
name order. A name claimed twice keeps its first entry, and the others go to
`catalog.shadowed` rather than being discarded.

The alternative is that the nearest file wins, as context files do, where the
nearest file has the last word. It loses on safety. Under first-wins, a cloned
repository's `.tau/prompts/deploy.md` cannot replace the user's own `/deploy`.
This is the same order `docs/EXTENSION-NAMESPACE.md` §3 chose for extension
commands. Dispatch order across kinds is separate: built-ins, then extension
commands, then templates and skills (§6).

### 2.3 Frontmatter without PyYAML

`tau-agent-core` depends on `pydantic` and `tau-llm` and nothing else. Adding
PyYAML to read a handful of keys did not seem worth the cost, so
`parse_frontmatter` reads a subset:

- `key: value`, plain, single-quoted or double-quoted;
- a plain value continued on indented lines;
- `|` and `>` block scalars, with `-` and `+` chomping;
- `#` comment lines.

The `>` form is the one the user's own `~/.claude/commands` files use. A key
holding a nested block, such as Agent Skills' `metadata:`, is recorded in
`nested` and not parsed. Reading it as text raises.

Anything else is refused, not guessed at. Flow syntax (`[a, b]`) is refused
because it would otherwise come back as the literal string `[a, b]`.
`allowed-tools: [Read, Bash]` therefore makes a template broken. This is the
one place where the subset is visibly narrower than YAML (see §9).

### 2.4 A broken file keeps its name

A file that cannot be read becomes a `BrokenResource`. Its name comes from the
file stem, or for a skill from the directory name. It occupies that name as a
working entry would, so:

- invoking it raises `ResourceError` with the reason;
- the completion list shows `broken: <reason>`;
- one bad file costs that entry and nothing else.

The rejected alternative was to drop the entry with a startup warning. Each
head would then need a new channel for that warning. Extension load errors are
already printed by five separate per-head loops, which is exactly the
"reached one head" shape. A broken entry needs no new channel. It shows up
where the reader is already looking: the completion list, and the error from
the submission that invoked it.

## 3. The trust-gate seam

`PROJECT_SCOPE_ADMITTED = {"prompts": True, "skills": False}` is the only place
that decides whether `<cwd>/.tau/<kind>/` is read. `resource_locations(...,
project=None)` reads the table, and an explicit `project=` overrides it. When
the trust gate (ROADMAP Tier 8, being designed in parallel) exists, it computes
this value per project and passes it in. No loader code changes.

Why project templates load now and project skills do not:

- **Templates.** A template enters the context only when the user types its
  name. τ already loads a project's `CLAUDE.md`/`AGENTS.md` and `.tau/SYSTEM.md`
  into every system prompt with no gate. A template that waits to be invoked
  can do strictly less than those can.
- **Skills.** A skill's description goes into the system prompt automatically,
  and its body tells the model to run scripts. In τ's current posture that is
  not more dangerous than a `CLAUDE.md`. However, the ROADMAP orders the skills
  leg behind the gate ("per the original plan"), so this build follows that
  order and does not decide the question for the gate. This is open question 1.

The project skills directory is not looked at, rather than found and reported
as skipped. That follows the precedent of `<cwd>/.tau/extensions/`, which is
also not read until the gate exists (`docs/extensions.md` §Discovery).

## 4. Templates: `tau_agent_core/prompt_templates.py`

### 4.1 Format

The format is Claude Code's custom-command format, which pi's template format
also matches. `<name>.md`: the file stem is the command word, the body is the
prompt, and the frontmatter keys read are `description` and `argument-hint`.
A missing `description` falls back to the first non-empty body line, cut to 60
characters. A directory of Claude Code commands therefore loads unchanged with
`--prompt-template ~/.claude/commands`.

The stem must match `[A-Za-z0-9][A-Za-z0-9_-]*`. `:` is excluded because
`skill:` and `ext:` are prefixes, and `.` because it separates an extension
from its command. A stem that fails this is a broken entry.

Other frontmatter keys are ignored. That includes Claude Code's `model` and
`allowed-tools`, as long as they are written in a form the subset can read
(§2.3). τ does not implement Claude Code's inline `` !`command` `` execution
or `@file` lines inside templates. Such text reaches the model as written (§9).

### 4.2 Arguments

- `$ARGUMENTS` / `$@`: what was typed after the name, **verbatim**. Quotes and
  spacing are kept, where pi re-joins its parsed words.
- `$1`…`$N`: shell-style words (`shlex`).
- `${N:-default}` and `${@:-default}`: a default when the value is empty.
- `${@:N}` and `${@:N:L}`: a run of words.

Substitution reads only the template, so a `$1` typed inside the arguments
stays as typed.

Two cases refuse, where pi substitutes an empty string:

- a bare `$N` beyond the words typed. "Make $1 with $2" given only `Button`
  would otherwise send "Make Button with ". The error names
  `${N:-default}` as the fix.
- an unbalanced quote when the template reads positions, since the words cannot
  be known. A template that reads only `$ARGUMENTS` never splits, so
  `/review don't` works.

When a body has **no** placeholder and arguments were typed, the arguments are
appended as `ARGUMENTS: …`. This is Claude Code's rule. pi drops them silently,
and raising would make the reader re-type a line for no real decision (an error
"that didn't require a real decision").

Known collision: a template containing shell like `awk '{print $1}'` has its
`$1` substituted. pi and Claude Code behave the same way, and there is no
escape syntax (§9).

## 5. Skills: `tau_agent_core/skills.py`

The format is the Agent Skills format that Claude Code uses: a directory
holding `SKILL.md`, where `description` is required and `name` is optional
(default: the directory name). The name must be lowercase words joined by
single hyphens, at most 64 characters. A description over 1024 characters makes
the skill broken. τ does not require `name` to match the directory name, which
pi also relaxes, because shared skill directories break that rule. An explicit
path can be a skill directory, a `SKILL.md`, or a directory of skills, one
level deep.

The model reaches a skill in two ways:

- **Listed.** `render_skills_block` writes `<available_skills>` with each
  skill's name, description and absolute `location`. `_build_system_prompt`
  places it after `<project_context>` and before the tool list, or wherever
  `{{skills}}` puts it. It is only written when a tool named `read` is offered,
  because the block's instruction is "use the read tool". A skill with
  `disable-model-invocation: true` is left out of the block.
- **Invoked.** `/skill:<name> args` sends the body inside
  `<skill name=… location=…>`, followed by a line naming the directory that
  relative paths resolve against, followed by `args`. The `skill:` prefix keeps
  skill names and template names from colliding.

The block is built when the backend is built, so it does not disturb the prompt
cache mid-session (`docs/PROMPT-CACHING.md`).

## 6. The one door

`_apply_input_pipeline` runs in this order:

1. `input` hooks;
2. if `expand_commands`: `resolve_command` (built-ins, then extension commands);
3. if no command matched: `expand_prompt_command`, which looks up templates, or
   skills for a `skill:` name.

An unknown name returns `None` and is sent as typed, which is the same
fall-through an unknown command already has. A broken entry or a refused
substitution raises `ResourceError` out of `submit()`.

Consequences, each of which needed no head code:

- **Persistence.** `submission_start` carries the post-pipeline text, so the TUI
  bubble, the persisted user node, and what the model received are one string,
  as an `input` hook transform already was.
- **Peek.** A head's peek (`resolve_command`) returns `None` for a template, and
  that is the correct answer: a turn is coming. `_submission_runs_a_turn`
  agrees.
- **Bus safety.** `expand_commands=False` keeps a bus or extension submission
  literal, so a NATS payload cannot invoke a template. This is the property the
  flag existed for.
- **Steering.** Steering runs the same pipeline, so a template typed mid-turn
  expands at delivery.

The listing goes through `AgentSession.get_prompt_commands()`. It drops a
template whose name a built-in or an extension command holds, since that
template is unreachable. `command_vocabulary` appends these rows with `origin`
`"template"` or `"skill"` and `flow: false`, so RPC `get_commands` and the serve
`Surface` list them. The TUI popup, the REPL Tab and `--connect` (from the
surface) pass them to `complete_command`.

## 7. `tau -p` persists what the turn built

Print mode no longer appends the typed text before the turn. It hands the
backend the stored context and appends every message the turn returned, in
order, including the core's user message. This fixes the three faults in §1.

Cost:

- A turn that raises before returning now leaves no prompt on disk. Before, it
  left a prompt with no answer.
- Two test fakes in `test_cli.py` and `test_headless_resume.py` now return the
  user message the way `TauBackend` does. Their assertions moved from "the
  context ends with the user turn" to `submission.text`.

`feat/code-mode-experiments` (`3a74ed7`) binds print mode's cursor. Once that
lands, the core persists the turn itself and this loop goes away.

## 8. Wire changes

- **RPC `get_commands` and serve `CommandInfo.origin`** gain `"template"` and
  `"skill"`.
- **Version.** Neither `PROTOCOL_VERSION` was bumped. The change is additive,
  but a client that validates the enum strictly will reject the new values.
  This is open question 3.
- **Audit.** `AgentSession.prompt_templates` and `.skills` are `NOT_EXPOSED`:
  a host needs the names, which `get_commands` gives it, and never expands
  anything itself.

## 9. What is absent

- **Project skills.** Waiting for the trust gate (§3).
- **Ancestor walk for `.tau/prompts`.** Only `<cwd>` is checked (§2.1).
- **Claude Code's `` !`cmd` `` and `@file` inside templates.** Not
  implemented. `` !`cmd` `` runs a shell command when the template expands,
  which is code execution, so it belongs behind the trust gate if it is added
  at all.
- **Escaping a literal `$1`** in a template body.
- **Frontmatter flow syntax.** `[a, b]` and `{k: v}` are refused (§2.3).
- **Reload.** Catalogs are read when the backend is built. A `/model` switch
  rebuilds the backend and so re-reads them. There is no `/reload`.
- **Palette entries.** The TUI's Ctrl+P palette does not list templates or
  skills. The completion popup does.
- **Default discovery of other tools' directories.** `~/.claude/skills`,
  `~/.claude/commands` and `~/.agents/skills` are each one config line away
  (`"skills": ["~/.claude/skills"]`), but they are not read by default. This is
  open question 2.
- **Extension-provided templates** (an `api.register_template`).
- **Re-using this loader for themes or extensions.** Themes have their own
  reader in `tau_coding_agent/themes.py`. Extensions load code, not Markdown.
  "Shared" here means shared by two kinds, and that is all it has been tested
  against.

## 10. Open questions

1. **Should project skills load before the trust gate?** In τ's current posture
   (`CLAUDE.md` is loaded ungated, and bash runs with no approval) they add
   nothing that is not already reachable. Answering yes flips
   `PROJECT_SCOPE_ADMITTED["skills"]` to `True`. If the trust gate will instead
   gate project *templates* too, the same table flips the other entry.
2. **Read `~/.claude/skills` and `~/.claude/commands` by default?** τ already
   reads `CLAUDE.md`. Answering yes means adding them to the user scope, after
   `~/.tau/`, so τ's own files win name collisions. The cost is every Claude
   Code skill's description in every τ system prompt, including ones that only
   make sense inside Claude Code.
3. **Bump the protocol versions for the widened `origin` enum?** RPC is
   MAJOR.MINOR, so 2.1 → 2.2 is cheap. Serve's hello refuses any mismatch, so
   0.8 → 0.9 breaks tau-code until it updates. Leaving both as they are breaks
   only a client that validates enums strictly.
4. **PyYAML.** A yes makes `parse_frontmatter` a thin wrapper, so §2.3's refusals
   disappear and flow syntax reads correctly. The cost is one more dependency of
   `tau-agent-core`.
