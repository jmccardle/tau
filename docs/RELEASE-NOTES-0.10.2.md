# v0.10.2 — shadowing stops being destructive, and three silent no-ops start working

Three fixes, one shape. In each case a value was written, stored, and read by
nobody — so the instruction reported success and did nothing. Nothing raised,
nothing logged, and the only way to find any of them was to go looking.

This is also the **last release published as a squash**. The section at the end
says what changes about this repository afterwards.

---

## An extension command keeps a name nothing can take

`ExtensionRegistry._commands` was one flat, unattributed `dict[str, dict]`, last
write wins. Measured with two extensions each registering `/note`:

| Gesture | Before | After |
|---|---|---|
| Disable the **shadowed** extension | deleted the **winner's** live command | winner keeps `/note` |
| Disable the **winner** | restored nothing | `/note` unbound; loser keeps `ext:<name>.note` |
| Reload the shadowed extension | silently stole the name back | binding unchanged |

A fifth fault of the same shape sat beside them, and it is the one worth
knowing: the flow table was keyed by the **typed** name, and `register_command`
never touched it. So a shadowing command inherited the shadowed one's
`FlowDeclaration`, and every head rendered one extension's argument form for
another extension's handler. Fully initialized, no error, wrong.

Three tables replace the one. `ext:<extension>.<command>` is minted at
registration and is uncontested. The typed name is a binding, and the only
contested thing. Flows are keyed by the **qualified** name, which closes the
fifth fault by construction — a binding of `/speak` cannot reach a flow declared
under a name its holder does not own. `Vocabulary.aliases` carries the bindings,
so a typed name still finds its flow: one `Flow`, two ways in.

**First-wins, reversing last-wins.** That is only defensible because the loser
keeps a name. There is no stack: disabling the holder leaves the typed name
unbound rather than falling back to whoever registered second — a name that
stops answering is easier to diagnose than a name that quietly changes meaning.

Composition is by name and late-bound. `api.run_command(qualified, args)`
resolves at call time, so a disabled extension is a name that stops answering
rather than a captured callable that still runs. `examples/61_command_wrapper.py`
is the demo. `docs/EXTENSION-NAMESPACE.md` is the record; §8 is what is absent,
including the `/ext:` completion grammar and the config key that would call
`pin_command`.

## A head honours `display=False`

`create_custom_message` has written `display` since the port and
`api.send_message` accepts it. Measured: `.display` had **no reader in
`tau_agent_core` at all**, and every match in `tau_coding_agent` was a Textual
widget's own attribute. An extension asking for a hidden node got a visible one.

`is_displayed` is that reader, and the scope is deliberately narrow — it is read
at the **transcript's** call sites only. A hidden node keeps its tree row, its
detail pane, its place on the active path, and its trip to the model when
`visibleToModel` says so.

The guard is **not** inside `add_persisted_message`, because
`TreeDetailPane._render_entry` calls that same method, and a guard there would
hide the node from the one surface that must keep showing it.

Older sessions do change appearance. That is the fix, not a regression.

### A `customEntry` row now says what it holds

`tree_browser.py` draws `node.preview or f"({node.kind})"`, so an empty preview
rendered as the literal word `(customMessage)`, and every non-`agent_spec`
`customEntry` as `customEntry: <type>`.

Three composers now name what the entry holds: `customType` leads, the bounded
part comes first because the row is cut to width, and a payload is summarized —
four fields named, the rest counted, a scalar cut at 40 characters, a container
rendered as a count. A reserved `extension_request` reads back as τ's framing
line plus the extension's sentence; a malformed one falls through to the generic
summary rather than raising.

`docs/EXTENSION-MESSAGES.md` is the record; §4 is what is absent.

## A view handed arguments refuses instead of dropping them

`/tree extra words` opened the tree browser and discarded `extra words`.

`docs/SLASH-COMMANDS.md` §4 listed this as the one unfixed Fail-Early violation
in its table, and predicted the fix would need a new `FRONTEND_COMMANDS` value
type. It did not. A view is a surface a head opens and carries no argument
string under any circumstances, so nothing about how views are declared had to
change: `dispatch_builtin` raises `UnsupportedCommandError` naming what would
have been dropped. That is the same refusal `/extensions frobnicate` already
made, and every head already renders it — **no head code changed.**

## A fourth of the same shape, found by the release gate

Not a shipped fix — a test one — but it is the same fault as the three above,
and it had been hiding for three releases.

`docs/RELEASING.md` has carried an unexplained 3.14 failure since 0.9.7: one run
in five reported `1 failed` and the command in use at the time did not print
which test. The section left an instruction behind — always `-rf --tb=line`,
record the name, fix the test, never add a retry. This release's matrix failed
the same way, and the instruction paid off:

```
FAILED tau-coding-agent/tests/test_app_actions.py::test_clicking_the_earlier_row_mounts_the_rest
textual/pilot.py:442: textual.pilot.OutOfBounds: Target offset is outside of currently-visible screen region.
```

**It is not a 3.14 defect.** Eight isolated runs of that test in a 3.14 container
on the release tarball passed. What the four Python versions differ in is how
long the other 6186 tests take to arrive; the failure needs the suite's load.

`await display.reload_messages(...)` returns when the build is **scheduled**,
not when it has landed. `_finish_build` runs a refresh later, clears the
`_building` flag, and then scrolls to the tail — and that scroll is deferred
again. A test scrolled a row into view inside that gap and clicked it, and
`pilot.click` resolves a widget to a screen offset, so a click is a question
about geometry that the build had not finished answering. The instruction
succeeded and nothing had happened, which is the shape of the other three.

Measured by constructing the losing schedule rather than waiting for it:

```
_building right after reload_messages returns = True
scroll_y after scroll_home(immediate=True)    = 0.0
after one pause: scroll_y=20  row=Region(x=2, y=-18, width=72, height=3)
```

against a screen of `Region(x=0, y=0, width=80, height=24)`.

The first fix was wrong and the gate caught it: it blamed the
`immediate: bool = False` that `scroll_home`, `scroll_to`, `scroll_end` and
`scroll_to_widget` all carry — a real default, recorded in
`docs/TRANSCRIPT-WINDOW.md` §7.2 for one member of the family, and not this
mechanism. With it applied the test failed again, on a different Python version.

`_building` turns out to be a fifth instance of the release's own theme: written
twice, read by nothing, since the window was built. It is now
`ChatDisplay.is_building`, the test waits for it *and* for the scroll position to
stop moving, and the failing assertion names the row's coordinates instead of
raising from inside Textual.

## Documentation

`CLAUDE.md` indexed 33 of 79 files in `docs/`, so 46 were reachable only by
`ls`, and the index was 73.6% of a file read on every request. It is now two
tiers: findings for the docs holding a trap, and a one-clause manifest for the
rest. Every file in `docs/` is now reachable by name, for 3 KB rather than 24.

Four claims in it were remeasured against the tree and were stale, including one
that named a spec directory retired in August and still cited by seventeen
source files.

Two research briefs with no referrers anywhere were removed. Twelve other
deletion candidates turned out to be cited from source by section number,
several from files named `*-PLAN.md` — the filename predicted nothing.

---

## After this release, the history here is the history

Until now `github.com/jmccardle/tau` has been a squash: one commit per release,
replacing the whole tree, with development happening in a separate repository
whose history was never published.

0.10.2 is the last release cut that way. From 0.10.3 the development history
**is** this repository's history — ordinary commits, pushed as they are made.

What that changes for a reader:

- `git log` becomes useful. Bisecting into a released version becomes possible.
- Commit subjects are written for the change, not for the release.
- `CLAUDE.md`, previously the one tracked file the public tree never received,
  is published from here on.

What it does not change: releases are still tagged `vX.Y.Z`, still published to
PyPI through Trusted Publishing, and the installable distributions are
unchanged. Nothing before 0.10.2 becomes reachable — that history stays private,
and this commit is the boundary.
