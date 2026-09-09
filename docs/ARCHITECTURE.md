# Architecture

Where things live and which way the imports point. Measured on
`architecture-overhaul` (2026-09-04), at the end of the overhaul.

`CLAUDE.md` holds the runtime story — how one turn flows from HTTP bytes to a
rendered tool call, and the two event vocabularies it crosses. This file holds
the static one: the packages, the modules, and the edges between them.

## 1. Packages

Five installable distributions, `src/`-layout. The first four stack bottom-up,
each depending only on the ones above it in this table; the fifth ships no code.

| Package | Imports as | Files | Lines | Owns |
|---|---|---:|---:|---|
| `tau-llm/` | `tau_llm` | 18 | 6794 | wire protocols, streaming events, message/tool types |
| `tau-agent-core/` | `tau_agent_core` | 60 | 33116 | agent loop, tools, sessions, extensions, compaction, the capability registry, the RPC verb table |
| `tau-coding-agent/` | `tau_coding_agent` | 24 | 16459 | the Textual TUI, the CLI entry points, the backends |
| `tau-jmfts/` | `tau_jmfts` | 9 | 2920 | the JMFTS client, catalog and enrichment extension |
| `tau-meta/` | `tau_meta` | 1 | 12 | nothing but `__version__` |

`tau-meta/` is alive and deliberately near-empty. It builds the `ffwf-tau`
distribution, which is a name resolving to `ffwf-tau-coding-agent[tui]` and
ships no functional code; its one module exists so that wheel's version is read
from a package attribute like every other distribution here rather than from a
second copy of the number in a pyproject, which `tests/test_packaging.py`
forbids. It is in the pre-commit hook's tree list and in no dependency, and
nothing imports it — `tau_coding_agent.__version__` is what the running program
reports.

The core is headless: `tau-agent-core` imports no Textual. The TUI is one head
on it, and `docs/HEADS-AND-MULTIPLEXER.md` prices the others.

## 2. The TUI modules

`app.py` was 9964 lines before the split. Eight modules now hold what it held —
the seven the split produced, plus `dialogs.py`, which the modal shell added
afterwards.

| Module | Lines | Owns |
|---|---:|---|
| `app.py` | 3217 | `TauApp` and four constants |
| `tree_browser.py` | 1793 | the session-tree screens, the row and zone algebra, the elide |
| `transcript.py` | 1802 | `ChatDisplay`, `MessageList`, the detail panes, lane rendering |
| `chat_widgets.py` | 1376 | `MessageBox`, `ChatInput`, `ChatSidebar`, the tool-call formatters |
| `editor_widgets.py` | 408 | the editor's own bars and popups (attachments, commands, lanes) |
| `modals.py` | 300 | the system-prompt, rollback and extension dialogs |
| `extension_ui.py` | 260 | the `ExtensionUI` delegate, its status bar and its panel |
| `dialogs.py` | 202 | `TauDialog`/`ChoiceDialog`/`TextDialog` — the frame, title, centering and cancel every modal inherits |

`backends.py` (1958) is the seam onto the core and did not move.

Four more modules in this package are a head and are **not** TUI modules: they
import no Textual — `test_repl_theme.py` imports `repl.py` in a fresh interpreter
and asserts `textual` never reached `sys.modules`.
They are listed here because the table above is where a reader looks for "what is
in `tau-coding-agent`", and a head that appears nowhere in it reads as absent.

| Module | Lines | Owns |
|---|---:|---|
| `repl.py` | 2404 | `run_repl`, `ReplLoop`, `ReplRenderer`, `ReplDelegate`, `ReplCompleter` — the REPL head (`tau --mode repl`, docs/REPL-HEAD.md) |
| `repl_input.py` | 494 | `LineReader` and its two implementations; the only module that imports `prompt_toolkit` |
| `repl_theme.py` | 113 | the REPL's one palette: a style and a glyph per role, and the foreign-lane badge |
| `steering.py` | 139 | `SteeringBuffer` and the `steering_strategy` config values, shared by the REPL and asserted equal to `app.py`'s copies |

Import edges among the eight, measured with `grimp`:

```
app            -> tree_browser, transcript, chat_widgets, editor_widgets, modals, extension_ui
tree_browser   -> transcript, dialogs
transcript     -> chat_widgets
modals         -> dialogs
session_picker -> dialogs
extension_ui   -> modals
extension_ui   -> app          (TYPE_CHECKING only)
```

Everything points down except that last edge. The delegate is constructed by
`TauApp` and holds it, so the annotation names `TauApp`; importing it at
runtime would make the cycle real. `grimp` still reports the 2-cycle, because
it reads the AST and a guarded import is an import there.

`dialogs.py` is the one module every screen-owning module points at and which
points at nothing, which is what a shell should look like from here.

Two 2-cycles predate the split and are real at runtime: `cli <-> headless` and
`cli <-> rpc_mode`.

## 3. The three largest modules in the tree

| Module | Lines |
|---|---:|
| `tau_agent_core/rpc/commands.py` | 3949 |
| `tau_agent_core/agent_session.py` | 3802 |
| `tau_coding_agent/app.py` | 3217 |

`app.py` is now one class. Splitting `TauApp` further means deciding what a
Textual `App` may delegate, which the split above did not have to answer, and
nothing here recommends doing it yet.

## 4. Where prose goes

`CLAUDE.md` § "Code style" is the rule. In short: a docstring says what a thing
is, a file under `docs/` says why it has that shape, and a comment is one line
about a dependence or an assumption. There are no multi-line comment blocks in
the `src` trees; `scripts/strip_comment_blocks.py` removed 13066 lines of them,
and `--check` in `.githooks/pre-commit` now fails a commit that adds one back.
It can be a hard gate where the docs-coverage gate deliberately is not: the
trees hold zero blocks, so it only ever fails on something a commit just wrote.

The one place prose is a load-bearing interface is `rpc/commands.py`'s module
docstring, which states the "E5 in Tier B" and "DURABILITY in Tier B" rules.
Twelve `notes=` strings cite them by name, those strings are published verbatim
into `docs/RPC-PROTOCOL.md`, and `test_rpc_tier_b_scaffolding.py` asserts that
every verb `COMMAND_TABLE` classifies is named by every list in them.

## 5. Decisions that were open and are now answered

Recorded here so the next reader does not re-derive them.

**A module-boundary checker: not yet, and the trigger is named.** The three
layering contracts all hold today, measured with `grimp` on 2026-09-04:
`tau_agent_core` imports no `textual` and no `tau_coding_agent`, and `tau_llm`
imports neither of the two above it. So `import-linter` or `tach` could be
switched on for those three now. What still fails on day one is the intra-TUI
seam contract — the `extension_ui -> app` edge in §2, which is
`TYPE_CHECKING`-only and real at runtime for neither reader nor tool, and the
two `cli` cycles, which are real. A checker goes in when that seam is fixed,
not before: a check that always fails is a check people switch off, which is the
same argument `docs/AGENT-DOCS.md` §7 makes about its own gate.

**Extension dependency manifests: no.** Already answered in prose in
`docs/extensions.md` §Discovery, and nothing since has changed it — dependency
management is the operator's venv, and a missing import surfaces as a load
error. Project-local `<cwd>/.tau/extensions/` discovery stays deferred behind
the trust gate for the same reason it always was.

**`scripts/symbol_graph.py` is permanent.** It answers a question the
module-level tools structurally cannot: `grimp`, `tach` and `import-linter` see
modules, and "does this class belong in the file it is about to land in" is
about the symbols inside one. It scored four candidate `app.py` split plans,
three of them distinctly, and its verdict on each held after execution. The
mover was a scratch one-shot and is not kept — `rope` is the mover.

## 6. Reference

- `CLAUDE.md` — the runtime pipeline, the commands, the conventions.
- `docs/HEADS-AND-MULTIPLEXER.md` — the core/head boundary and what a second
  head costs.
- `docs/VSCODE-HEAD.md` — one head priced in detail, including the two blockers
  (no reverse channel, the tree is not on the wire).
- `docs/AGENT-DOCS.md` — how `docs/library/reference/` is generated.
