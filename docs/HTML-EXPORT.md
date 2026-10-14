# A session as one HTML page

Built 2026-10-12. `tau --export-html REF [OUT]` writes a saved session, every
branch of it, as a single HTML file that loads nothing from the network. The
rendering is `tau_agent_core.export.render_session_html`, a pure function of
`(entries, header, leaf, config)`, so any head can call it.

## 1. What was there

ROADMAP Tier 9 said "no HTML exporter is reachable from the CLI". That was true,
and it left out the larger finding: an HTML exporter **existed** and was reachable
from nothing at all. `export.HTMLExporter` was exported from the package root
and had no caller in any `src` tree. The only reader was its test file. Run
against real data, it had four defects:

- **Every reasoning block rendered empty.** It read a thinking block's `text`
  key. `ThinkingContent` stores `thinking`, and so does every provider and
  `agent_loop.py:915`. `MarkdownExporter` had the same bug. The tests passed
  because their fixture built thinking blocks with `text` as well, so the
  fixture agreed with the bug and not with the data.
- A tool call printed its arguments twice, joined by a literal backslash-n
  (`\\n` inside an f-string).
- Images were dropped, both in user messages and in tool results.
- Its stylesheet styled `.message`, a class no element carried.

It also took a flat message list, so it could not show a tree. A session with
one fork lost one branch, whichever way the list was built.

Measured on three real sessions with the new renderer (`~/.tau/sessions`,
2026-10-12): 311 entries with 1 compaction, 1 branch summary and 9 inline images
came to 2.4 MB in 25 ms. 23 entries with 3 forks came to 34 KB in 1 ms. An 11-entry
session holding one pasted screenshot came to 165 KB. Every page parsed with
balanced tags, held no `<script>`, and had no `src`/`href` pointing anywhere but
`data:` or `#`.

## 2. Static HTML, no script

pi's exporter (`packages/coding-agent/src/core/export-html/`) embeds the session
as JSON and draws it in the browser with ~1900 lines of `template.js` plus
vendored `marked` and `highlight.js`. That design gets interactive tree
navigation and rendered Markdown. It costs a JavaScript renderer τ would have
to maintain beside the Python one, and a page that shows nothing with scripts
disabled.

τ renders on the Python side. The page has no script at all, and a
`Content-Security-Policy` meta (`default-src 'none'; img-src data:; style-src
'unsafe-inline'; base-uri 'none'; form-action 'none'`) makes "fetches nothing"
something the browser enforces, so it does not rest on the renderer escaping
correctly. `base-uri` and `form-action` are named because `default-src` does
not cover them. Escaping is still tested on its own: every string a session
carries, from message text to entry ids, model ids and `customType`, is fed a
tag-and-attribute payload, and the parsed page may hold only the renderer's own
tags. Images are `data:` URIs.
Folding uses `<details>`, which needs no script. Colours are `:root` tokens with
a `prefers-color-scheme: dark` override.

**Message text is preformatted, not rendered Markdown.** It sits in
`white-space: pre-wrap`. A Markdown renderer means either a dependency in
`tau-agent-core`, which has none for this, or a hand-written one, which is a
second Markdown dialect. Preformatted text is exactly what the model wrote.

## 3. Laying out a tree

Every entry appears exactly once. One path is the **main thread**: the path to
`leaf`, which defaults to `session_log.default_leaf`, the place a reopened
session continues. At each fork the other children fold in place, right after
the fork entry and before the thread continues, as `<details class="branch">`
labelled with the branch's size and first line.

Inside a folded branch the same rule repeats, but with no leaf to follow:
**the child whose subtree holds the newest-written entry continues inline**.
That is the child someone last worked on. The alternative, oldest-first (the
order `ConversationTree.tree()` sorts children in), would put an abandoned
first attempt inline and fold the retry.

Children of the exported leaf itself all fold. Nothing continues past the
position the page says the session continues from. The leaf carries a
"session continues here" badge.

The walk uses an explicit work stack, so a long linear conversation costs no
recursion depth. The test renders 3000 entries in a chain. Branch nesting adds
stack entries, not Python frames.

The page is a record of the **tree**, not of what the model saw. A compaction
is a card showing its summary and the span it folds (the tree browser's own
`_splice_anchor_preview`, read through `TreeNode.preview`), and the entries it
folded still appear above it. `ConversationTree.context_for` is the model's
view, and it is a different document.

## 4. What each entry becomes

- `message` / `customMessage`: a section per role. Reasoning is a collapsed
  `<details>`. A tool call shows its name, id and pretty JSON arguments. A tool
  result is preformatted output, and starts folded past 24 lines. An error
  result is tinted. The system prompt is collapsed. An extension message is
  labelled with its `customType`, plus "hidden from transcript" when it set
  `display: False`. The assistant header shows model, any stop reason other than
  `stop`/`toolUse`, and token usage.
- `compaction` / `branch_summary` / `elide`: a summary card.
- `session_info`, `model_change`, `thinking_change`, `navigate`, and `config` /
  `agent_spec` custom entries: one muted line from `TreeNode.preview`.
- **Anything else is drawn raw, never dropped.** Another `customEntry` gets its
  line plus a folded JSON dump of `data`. An unknown kind, such as the JMFTS
  store's `jmfts:document`, gets a folded dump of the whole entry. The same goes
  for a content block of an unknown type, and for a `content` that is neither
  a string nor a list. An image whose `mime_type` is not `image/<subtype>`, or
  whose `data` is not base64, is reported in a line and not embedded. A system
  prompt's non-text blocks are drawn after its folded text.
- An interrupted (`status: incomplete`) entry carries an "interrupted" badge.

`ExportConfig`'s three switches still apply. `include_tool_calls=False` drops
both the call and the result. An assistant message left empty by that is
dropped too, rather than drawn as an empty box.

## 5. The CLI

`tau --export-html REF [OUT]`:

- REF is a `.jsonl` path, a session id, or a unique id prefix, resolved in
  **every** directory (`resolve_ref(cwd=None)`). The cwd scoping on `--session`
  stops a run from resuming another project's conversation. An export only
  reads, so that scoping protects nothing here and would only get in the way.
- OUT defaults to `tau-session-<id>.html` in the current directory, and `-` writes
  to stdout. An existing file is overwritten, the way `cp` does it.
- `--store` and `--session-dir` are honoured: they say which store REF is looked
  up in. Every flag the export would ignore is refused, with one error naming
  the set: `-p`, messages, `--mode`, `--connect`, the continuation flags,
  `--no-session`, and the JMFTS copy flags.
- An argparse trap: OUT has to come straight after REF. Written after another
  flag, it parses as a positional message, and the command is refused for having
  one. That error is loud, not silent, so it was left as it is.

The name is `--export-html` and not pi's `--export`, because `--export-session`
already exists and does something unrelated (a JMFTS→`.jsonl` copy). Two flags
named `--export` and `--export-session` with nothing in common would be a trap.

## 6. What is absent

- **No TUI `/export` command.** A slash command is a capability-registry entry
  (`tau_agent_core.capabilities`) that every head must perform or refuse, and
  RPC's `get_commands` advertises it. That is a cross-head change, not a TUI
  one. Until then the TUI user runs `tau --export-html <id>` from a shell.
- **No RPC verb.** A verb is a protocol bump (1.x MINOR, the way 0.10.1 added
  `get_tree`), which is release-level. Calling the library function is one line
  for a host in-process. An out-of-process host can already read the whole tree
  through `get_tree` and `get_entry`.
- **No Markdown rendering and no syntax highlighting** (§2).
- **No tree export to Markdown.** `MarkdownExporter` is still flat. It had the
  same thinking-key bug, which is fixed here, but it was not taught branches.
- **No extension tool renderers.** pi lets a tool pre-render its own HTML. τ
  has no renderer hook for any head (`EXTENSION-MESSAGES.md` §4 records the same
  absence for the transcript).
- **No size cap.** Images are embedded at full size, so a session full of
  screenshots makes a large page. The measured worst case above is 2.4 MB.
