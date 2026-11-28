# D34: Command hooks (the `hooks` built-in)

**Status:** accepted 2026-11-25, built for 0.18.0 (released 2026-11-28); §12 added by ruling D-2. Amends D19 §4 (a fifth pinned file,
`.tau/hooks.json`) and D24 §1 (a built-in that is off by default, `hooks`). Companion: the guide,
`docs/guide/built-ins.md` ("Run your own commands at hook points").

## Context

Claude Code, Codex and Gemini CLI let a user run a shell command at points in an agent's work. The
shape is the same in all three: a command; the event as JSON on its standard input; an exit code
(2 blocks, and standard error is the reason) or a JSON object on standard output for the answer.
There are many hook scripts written for it, and a user who moves to τ should keep them. τ has the
points (`pre_tool`, `pre_result`, `pre_input`, `post_step`, `start`, `stop`) but no way for a
person to attach a command to one without writing Python.

Constraints: akb #643 (administrator configuration lives in files, never in an option a client
can set), D19 §4 (a project file that runs code is pinned by `tau trust`), Fail Early (a decision
τ cannot honor is not dropped), and that the `bash` tool is unavailable on Windows.

## Decision

1. **A built-in, off by default**: key `hooks`, class `tau.CommandHooks`, `load: {hooks: true}` or
   `tau ext enable hooks`. Its one option is `abort_grace` (§12). Its commands are in `hooks.json`
   files, never in config.
2. **The file is the `hooks` key of a Claude Code `settings.json`**: `{"hooks": {EVENT: [{"matcher",
   "hooks": [{"type": "command", "command", "args", "timeout", "onFailure"}]}]}}`. `$TAU_HOME/hooks.json`
   then the trusted project's `.tau/hooks.json`; a repeat runs once. Read when τ starts; a file with
   any problem stops the start, listing every one. Unknown top-level keys are errors.
3. **Events** (rows are the mapping): `PreToolUse` → `pre_tool`; `PostToolUse` and
   `PostToolUseFailure` → `pre_result` (by outcome: `Ok` and `Err`; a call a hook refused is not
   reported); `UserPromptSubmit` → `pre_input` for a `Frontend`'s input; `Stop` → `post_step` when
   a *person's* turn ends (a reply with no call, committed by this step, whose newest non-hook
   `User` node is a `Frontend`'s, so sub-agents and summarizers never run it); `SessionStart` and
   `SessionEnd` → `start` and `stop`. Every other Claude Code event is a load error naming it.
4. **Matcher** (tool events): Claude Code's syntax (empty/`*`, exact names split on `|` `,`, else a
   regular expression, here Python's `re`, tried on a tool's τ name and its Claude Code name);
   also Claude Code's names for τ's base tools (`Bash`→`bash`, ...), and no others: τ is not
   Claude Code. A matcher on an event with no tool is an error (Claude Code ignores it). A name no
   tool of the session has, and a permission rule (`Bash(git *)`), can never match: a warning when
   the first input arrives, where the assembled tools are known (`mcp__` names are exempt: a server
   may list its tools later). `tool_name` and `tool_input` are τ's own; the fields differ from
   Claude Code's for τ's file tools, which is stated, not hidden.
5. **Answers.** Exit 0: JSON object (a stdout starting with `{` that does not parse is a failure) or
   text (context at `UserPromptSubmit`; dropped, said on stderr, at `SessionStart`; unused elsewhere).
   Exit 2: block, standard error the reason; at the post events, feedback; at the session events, a
   failure. Other exits, 1 included: a failure. JSON fields supported: `permissionDecision`
   (`allow`, `deny`, `ask` as a yes/no question to the person, `defer` unsupported),
   `permissionDecisionReason`, `updatedInput` (the tree keeps the model's arguments, so the
   result starts with `[hooks rewrote the arguments: ...]`, as `bash_guard` says what it ran),
   `decision` (`block`; `approve` at `PreToolUse`),
   `reason`, `additionalContext`, `updatedToolOutput` (text), `systemMessage`, `suppressOutput`.
   `continue: false`, `stopReason`, and every other field are failures naming the field, and so is
   a supported field at an event that does not take it.
6. **Order and combination.** The commands of an event run one after another in file order, the
   first block stops the rest, and a later command sees an earlier `updatedInput`: Claude Code
   runs them in parallel, which leaves an `updatedInput` race. `pre_input` is at -100 and `pre_tool`
   at -150 (before `permissions`' -100, so rules judge the rewritten arguments); `pre_result`,
   `post_reply` and `post_step` at +100.
7. **Failure policy.** A command that does not start, passes its `timeout` (default 60 s; 20 s at
   `SessionStart`, 4 s at `SessionEnd`; its process group is killed), writes over 1 MiB to a
   stream, exits other than 0 and 2, or answers what τ cannot honor, is *told* (an `error` notice
   from `hooks` naming the command; standard error for the session events, where no client is
   connected). At a gate (`PreToolUse`, `UserPromptSubmit`) it also *blocks*, unless the handler's
   `onFailure` is `continue` (Claude Code's own key). At `PostToolUse*` and `Stop` there is nothing
   to refuse and it only reports. Claude Code lets a call through on exit 1; τ does not.
   A process group is ended by SIGTERM, then SIGKILL after 2 s: at a timeout, when the hook is
   cancelled, and for every command still running at the extension's `stop`. Once the command
   itself exits, τ takes what it wrote (a 0.1 s drain) and goes on; a child it left in the
   background is left alone, as Claude Code leaves it.
8. **Stop** sends the reason as an input authored by `Extension("hooks")`, so the model continues.
   `stop_hook_active` says the last stop was already held open; after 8 in a row (Claude Code's cap)
   the turn ends with a warning; with auto-step off the hook is told and the turn ends.
9. **Trust.** `.tau/hooks.json` is the fifth pinned member of a project (`hooks_sha256` in
   `trusted.json`; `Trusted.hooks_file` holds the approved bytes, which the extension runs). The
   report shows the whole file, never cut, and a change asks again; a file that is not a hooks file
   cannot be approved; symlinks and non-regular files are refused. An older `trusted.json` record
   without the hash matches while the project has no hooks file.
10. **Windows.** Unavailable: commands run through `/bin/sh`, which τ does not have there (the
    `bash` tool's reason). Enabling it fails naming the platform; its tests are skipped there.
11. **Not sent**: `session_id`, `transcript_path`, `permission_mode`, `effort`, `agent_id`,
    `agent_type`, `prompt_id` (the extension is built before a session directory is chosen).
    `tool_response` is the result's text, a string (Claude Code sends an object); `source` is
    always `startup` and `reason` always `other`. Not supported: the other 27 events, the handler
    types `http`, `mcp_tool`, `prompt`, `agent`, and the keys `if`, `async`, `asyncRewake`,
    `shell`, `statusMessage`, `once`.

12. **Abort** (ruling D-2). When a step is aborted (Esc, a client's abort), a command still
    running at one of its hook points gets
    `abort_grace` seconds (option of `hooks`, default 2, not negative). After that its process
    group is ended as at a timeout (SIGTERM, SIGKILL 2 s later) and the failure is told (`was still
    running 2 s after the step was aborted: killed`). A command that ends inside the grace is left
    alone. Abort still never cancels a hook (model.md §8, Q34): the hook learns of the abort
    through `TreeHandle.aborted()`, which returns once its own cursor is aborted or sent elsewhere,
    and cancels only the command it runs. The step is aborted either way, so a block or answer
    that comes late changes nothing. `SessionStart` and `SessionEnd` are no step, and are not
    reached; the shutdown path (§7) ends their commands.

## Rejected

- **Commands as options under `extensions.hooks`.** A client can write an option (`/config`); a
  hook is a command the administrator chose (akb #643). Only `abort_grace` (§12) is an option: it
  names no command, and the most a client can do with it is shorten or lengthen a wait.
- **Parallel commands.** Faster, but two `updatedInput`s race, and a block's reason is whichever
  finished first.
- **Letting a call through when a hook fails.** A guard that cannot run would not guard; the escape
  is an explicit `onFailure: "continue"`.
- **Translating tool names and arguments to Claude Code's** (`file_path`, `Bash`): a translation
  of `updatedInput` back is a second source of bugs; τ states where the shapes differ.

## Open

- `session_id` and `transcript_path` would need the session directory handed to extensions.
- A `SessionStart` hook's output as context would need a `System` node written at the first step
  (`skills` does this for its list).
- `PreCompact`, `SubagentStop` and `Notification` could map once those have hook points.
