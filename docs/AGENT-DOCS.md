# The agent-facing documentation library

**Status: the mechanism is built (2026-08-26). The prose half of the library is
not yet written, and the coverage gate is not yet in the pre-commit hook.**
§7 records the exact remaining work and the measured baseline.

τ ships documentation an agent reads, with the ordinary `read` tool, in order to
extend or modify τ itself. This document is the design of the machinery that
produces it, and the record of what it currently produces.

---

## 1. Why this exists at all

pi ships its `docs/` directory inside the npm package, resolves the install path
at runtime (`getPackageDir()` → `getDocsPath()`), and writes three absolute
paths plus a hand-written topic→filename routing table into the system prompt.
The agent reads the files with `read`. There is no docs tool, no index and no
retrieval. Its startup hint — "Pi can explain its own features and look up its
docs" — is backed by ~12,000 lines of prose in that directory.

τ has the opposite shape. It has 8,359 lines of test-locked example code under
`examples/` and roughly 1,650 lines of prose, and that prose answers "why is it
like this" rather than "how do I add one". Five surfaces have no page at all,
including the system-prompt placeholder system, whose only documentation is the
docstring of `_build_system_prompt`.

So τ needs a library, not a port of pi's plumbing. This document covers the half
of that library a machine can produce: **the reference section, generated from
the source**.

## 2. One source, two renders

Every page in the library is a flat markdown file. Each `##` section carries an
audience marker on the line directly after the heading:

```markdown
## Registering a tool
<!-- agent: yes -->
```

The human render is exhaustive; the agent render keeps only the sections marked
`yes`, dropping the prose, diagrams and rationale that cost context without
changing what an agent does. One source, two filters.

The generated reference emits the marker on every top-level section, so a single
filter serves both halves of the library.

`scripts/export_site_reference.py` (§9) does **not** strip the markers on its way
to the site. They are HTML comments, so they render as nothing, and passing them
through is what keeps an exported page's body byte-identical to its
`docs/library/reference/` counterpart — which is a property a test can hold, and
"looks the same once rendered" is not.

## 3. The marker

`tau_llm/docs.py` defines `@agent_facing(topic=..., since=...)`. It lives at the
bottom of the package stack so all four distributions can import it.

It is a **complete** no-op. It returns the decorated object unchanged, typed as
the identity function so mypy infers exactly what it did before, and leaves no
trace on the object at all. It never wraps and costs nothing on a call.

Leaving no trace was not the first design, and the reason it is now the design
is worth recording. The first version set a `__tau_agent_facing__` attribute
holding the marker's arguments, for callers who might want to introspect a live
object. On a `Protocol`, that attribute joins `__protocol_attrs__` — so it
becomes a member every implementation must have, and `isinstance` starts
rejecting classes that satisfy the protocol perfectly well.
`tau_agent_core.session_catalog.ConversationSession` is such a protocol, and
`AgentSessionRuntime.fork()` isinstance-checks against it; marking it broke
`test_fork_carries_history_and_leaves_the_source_untouched` in the full suite.

A documentation marker must not be able to change what the program does, and the
only way to guarantee that is for it to do nothing. The attribute was also a
second source of truth that earned nothing: the build never read it, because the
build never imports. `test_marking_a_protocol_does_not_change_isinstance` holds
the line.

**Which objects carry it.** Any object an extension author or an agent calls.
That set is deliberately not `__all__`. `__all__` means "public API", and the
two differ in both directions: `_build_system_prompt` is private and is exactly
what an agent extending τ needs, while `PromptLatencySample` is public and no
extension touches it.

**Marking a class marks its public members.** A method may carry its own marker
to file itself under a different topic; it does not need one to be documented.
Private names are skipped. Dunders are skipped **except** the ones that define
how a caller uses the object — `__call__`, `__enter__`, `__aiter__` and the rest
of `_PUBLISHED_DUNDERS`. `__post_init__` and `__repr__` are not documentation.

**Topics are closed.** `docs_build.TOPICS` is the only place a topic is
declared. A marker naming a topic that is not in it raises `UnknownTopicError`.
Adding a topic is a decision about the shape of the library — an entry in
`TOPICS` plus a prose page — not a new string typed into a decorator.

## 4. griffe, and what τ did not write

The build reads the source **statically**. It imports none of the code it
documents. `tau_agent_core.docs_build` calls `griffe.load(...,
allow_inspection=False)`, which parses the AST and never falls back to an
import.

That property is not a nicety here. τ's Anthropic and Google providers import
their SDKs lazily so the test suite runs without either installed, and importing
`tau_coding_agent` pulls in Textual. A runtime decorator registry — the obvious
DIY design — would need every one of those modules imported to find its markers,
and would give up both properties.

The decorator and griffe are complementary rather than alternatives, because
**griffe reads the decorator out of the AST**. `decorator.callable_path`
resolves to `tau_llm.docs.agent_facing` for all three import forms:

```python
from tau_llm.docs import agent_facing        # @agent_facing(...)
import tau_llm.docs                          # @tau_llm.docs.agent_facing(...)
from tau_llm import docs as d                # @d.agent_facing(...)
```

`tests/test_agent_docs.py` asserts all three, against a module that cannot be
imported at all (it imports a package that does not exist), so the static claim
cannot pass by accident.

### What was evaluated and rejected

| Considered | Verdict |
|---|---|
| Sphinx `autodoc`, `pdoc` | Rejected. Both import the module under documentation. |
| `__all__` as the marker | Rejected. It means "public API", a different set — see §3. |
| `pydoclint`, `darglint` | Not needed. griffe already reports `Parameter 'x' does not appear in the function signature`. The gate promotes that warning to an error rather than adding a tool. |
| `interrogate`, `docstr-coverage` | Rejected. Wrong denominator: they measure every object, and the metric wanted here is scoped to the marked set. |
| `mkdocstrings` | Adopt later, for the **site** render only. It consumes the same griffe data, and the site is already MkDocs Material. Not wired up yet. |
| `griffe check` | Worth adopting separately. It diffs two versions and reports API breakage, which is a second use for a dependency already present, and release notes are written by hand today. |

## 5. What the build produces

```bash
venv/bin/python scripts/build_agent_docs.py     # writes docs/library/reference/
venv/bin/python scripts/check_docs_coverage.py  # the gate; exits non-zero on a fault
```

All the rendering lives in `tau_agent_core.docs_build`, which is pure — it takes
paths and returns strings. The scripts are thin CLIs that write to disk. This
follows `tau_agent_core.rpc.protocol_doc` and `scripts/generate_rpc_protocol_doc.py`.

The output is checked in, so a change to a marked docstring appears in the diff
of the pull request that made it. `tests/test_agent_docs.py` asserts the
checked-in files match a fresh build, exactly as `test_rpc_protocol_doc.py`
guards `docs/RPC-PROTOCOL.md`.

**An undocumented parameter is still published.** Its name, annotation and
default come from the signature, not from prose, so the reference never silently
omits an argument — an object with no docstring at all still conveys what it
takes. What the reader loses is the meaning, and that is marked in the output as
`*(no description)*` rather than left blank. The coverage gate is what stops the
meaning from going missing quietly.

## 6. The coverage gate

The rule, from `CLAUDE.md`:

> Any object an extension author or an agent calls carries `@agent_facing`. A
> marked object with an undocumented parameter fails
> `scripts/check_docs_coverage.py`, which the pre-commit hook runs alongside
> mypy and ruff.

There is no threshold and no ratchet, on purpose. The marked set is opt-in, so
100% is the only defensible bar: marking an object is a claim that an agent will
call it, and a claim with no prose behind it is the silent omission the library
exists to prevent. An object that should not be documented has its marker
removed — a visible decision in a diff. Lowering a number is not.

Four faults fail the check:

1. A marked object with no docstring at all.
2. A marked object whose docstring omits a parameter.
3. A callable annotated to return something, with no `Returns:` section.
4. A griffe docstring warning, chiefly docstring/signature drift.

A fifth case fails too: **finding nothing at all**. An empty marked set means
the marker was removed, the packages moved, or griffe stopped resolving the
decorator — and a naive percentage would report 100% for all three.

## 7. Status, measured 2026-09-04

220 markers cover the headless surface: `tau_llm` and `tau_agent_core`.
`tau_coding_agent` is not marked — every topic in `TOPICS` is a headless one,
and an extension author reaches the TUI through `ExtensionUI` rather than
directly. Marking it means adding TUI topics first.

Those markers pull in 924 objects once class members are counted.

| | 2026-08-26 | 2026-09-04 |
|---|---:|---:|
| Complete | 316 / 758 (41.7%) | **481 / 924 (52.1%)** |
| No docstring | 245 | 246 |
| Undocumented parameter | 177 | 165 |
| Missing `Returns:` | 140 | 124 |
| Docstring/signature drift | **0** | **0** |

The marked surface grew by 166 objects over the same period, so the percentage
moved on completions outpacing additions rather than on a static denominator.
`CLAUDE.md` quoted the 41.7% figure until 2026-09-04 and is now current; if the
two disagree again, the gate's own output is the authority — run
`venv/bin/python scripts/check_docs_coverage.py`.

Drift started at 12 and is now zero. Those twelve were four real defects:
`ApiFactory` documented `__call__`'s parameters on the class docstring, where
they matched no signature; `EventBus.emit_channel` left `*args`/`**kwargs`
unannotated; and two `Raises:` sections ended with a prose sentence that is not
an `Exception: description` pair. All four are fixed.

### What remains

1. **Write the missing prose.** 535 faults. This is the bulk of the work and is
   deliberately not scoped as a sweep: `CLAUDE.md` §"Code style" makes it
   opportunistic instead — editing a function means leaving its docstring
   shorter and more useful in the same commit, permission standing. Filling a
   missing docstring is the fastest way to raise this number, and shortening a
   long one cannot lower it, because the gate counts complete and never counts
   length.
2. **Wire the gate into `.githooks/pre-commit`.** Deliberately not done: a hook
   that fails on every commit is a hook people switch off. It goes in when the
   tree is clean. The debt is stated here rather than hidden behind a threshold.
3. **Ship the pages inside the wheel.** `tau-coding-agent/pyproject.toml`
   currently declares only `tau_default_config.json` and `tau.tcss` as
   package data. Shipping needs the reference copied into the package, an
   `importlib.resources` lookup with a `TAU_DOCS_DIR` override, and a
   `{{tau_docs}}` system-prompt placeholder holding the ~12-line index.
4. **Write the prose pages.** The reference answers "what does this take". The
   twelve hand-written pages that answer "how do I add one" are the other half
   of the library and are not started.
5. ~~**Strip the markers in the site build.**~~ Declined 2026-09-05, when §9's
   exporter was written. The markers are HTML comments and render as nothing, so
   the hook would buy no visible difference and would cost the body-identity
   property §9 rests on.

## 8. Files

| Path | What it is |
|---|---|
| `tau-llm/src/tau_llm/docs.py` | The `@agent_facing` marker. No dependencies. |
| `tau-agent-core/src/tau_agent_core/docs_build.py` | Collection, coverage and rendering. Pure. Needs griffe. |
| `scripts/build_agent_docs.py` | Writes `docs/library/reference/`. |
| `scripts/check_docs_coverage.py` | The gate. |
| `scripts/export_site_reference.py` | Writes the same pages into the MkDocs site. §9. |
| `tau-agent-core/tests/test_agent_docs.py` | 17 contract tests; see its module docstring. |
| `tau-agent-core/tests/test_site_export.py` | 8 contract tests on the export; see its module docstring. |
| `docs/library/reference/*.md` | Generated. Checked in. Do not edit. |

## 9. The site export

`../ffwfrobotics.github.io` publishes τ's documentation to the web. Five prose
pages under `docs/tau/reference/` say how the packages fit together and which
distinctions are easy to get backwards; they are hand-written and stay that way.
The generated reference goes beside them, not over them:

```bash
venv/bin/python scripts/export_site_reference.py --site ../ffwfrobotics.github.io
venv/bin/python scripts/export_site_reference.py --site ../ffwfrobotics.github.io --check
```

Fourteen pages land in `docs/tau/reference/api/`, one per topic plus an index.

**The body is byte-identical to `docs/library/reference/`.** The export renders
through `render_topic` and then replaces only the title line, the banner and the
frontmatter, so there is one renderer and two heads rather than two renderers
that can disagree. `test_site_export.py` asserts it line by line.

**The site path has no default.** This repository does not know where anyone
else keeps a checkout, and a guessed relative path that happens to exist is how
a script writes into the wrong tree. A `--site` that is not the documentation
site — no `mkdocs.yml`, no `docs/tau/reference/index.md` — is refused before
anything is written.

**The nav is maintained, not hand-copied.** `mkdocs.yml` carries a marker comment
pair inside the Tau `Reference:` node and the export replaces the lines between
them, at the markers' own indentation. Everything else in that file survives byte
for byte, which is why this is line surgery and not a YAML round-trip: the file
has comments, and a round-trip loses them. A `mkdocs.yml` with no markers is an
error naming the two lines to add — never a set of pages that appear in no menu.

**Nothing is deleted that this script did not write.** The stale sweep only
removes files under `api/` carrying the export's banner; anything else there
stops the run.

**`--check` writes nothing** and exits 1 listing every page that is missing,
stale or orphaned, plus the nav if it moved. No test in this repository can hold
the site current, because the site is another repository — `--check` is what does
it, run in the checkout that has both.

Two things this export does not do. It does not publish a version-pinned copy:
the pages describe the working tree, and the τ version appears only on the index
so that a version bump does not re-write all fourteen files. And it does not link
an object to its source: the public repository squashes history per release
(`docs/RELEASING.md`), so a line number is only addressable against a release
tag, which the export does not know it is standing on.
