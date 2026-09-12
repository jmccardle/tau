# v0.10.3 — an extension can import the module beside it, and this history is public

Two changes, and the second one is about this repository rather than about τ.

---

## `import helper` works from a single-file extension

An extension written as one file could not import a module sitting next to it:

```
/proj/myext.py      import helper   ->  ModuleNotFoundError: No module named 'helper'
/proj/helper.py
```

It was tempting to read this as a working-directory problem, and it is not.
`tau` is a **console script**, so `sys.path[0]` is the venv's `bin/` directory
and the working directory is never on the path at all. Measured from the
extension's own directory:

```
--- cwd = /proj ---
FAIL /proj/myext.py: ModuleNotFoundError: No module named 'helper'
OK   /proj/pkgext:   'inner reached'
```

So the failure was total, not conditional. A **directory** extension was always
fine — `spec_from_file_location` gets `submodule_search_locations`, which is
what makes `from .helper import x` resolve — and that asymmetry is what made the
single-file case look like bad luck rather than a missing feature.

`_load_one_extension` now prepends the module's own directory to `sys.path` for
the length of the import and pops it in a `finally`.

| | before | after |
|---|---|---|
| `/proj/myext.py` → `import helper` | fails everywhere | works |
| `/proj/pkgext/` → `from .inner import x` | works | works |
| `/proj/pkgext/` → `import inner` | fails | works |
| `sys.path` after the load | unchanged | unchanged |

**The window is the module body and nothing else**, which is narrower than it
could be and deliberately so. That span holds no `await`, so no other extension
load can splice its directory in while this one is open; widening it over
`register(api)` would open exactly that hole, because that call *is* awaited and
two sessions load extensions in one process. One extension's directory answering
another's import is a silently wrong module, which is worse than the
`ModuleNotFoundError` it would replace.

The cost is that an import deferred into `register` or into a hook still fails —
loudly, and exactly as it does today. **Put sibling imports at the top of the
file.** The entry is *prepended*, matching what Python does for a script, so a
module beside the extension beats an installed distribution of the same name;
avoid naming a helper after something on PyPI.

Two of the five new tests fail without the fix. The other three pin what the
docstring claims: `sys.path` is restored after a clean load, restored after a
raising one, and a deferred import still raises.

This unblocks a τ head that ships its own Python runtime and launches it with
`-I`. That flag removes the user's site directory from `sys.path`, which is
worth doing on its own — an unrelated `typing_extensions` in `~/.local` was
shadowing the bundled one and surfacing as `ImportError: cannot import name
'sentinel'` inside a model streaming call, nowhere near its cause. Extensions
are the one place where the host's runtime and the user's project legitimately
meet, so that is where the isolation had to be paid for, and it is paid here
rather than worked around downstream.

`docs/extensions.md` "Discovery" documents the rule and its two limits.

## This repository's history is now public

0.10.2 was the last release published as a squash. From this one, the
development history **is** the public history: commits go to
`github.com/jmccardle/tau` as they are made, unsquashed.

- `git log` is useful, and bisecting into a released version is possible.
- Commit subjects are written for the change, not for the release.
- `CLAUDE.md`, previously the one tracked file the public tree never received,
  is published — and `test_no_host_addresses.py` scans it as prose, with an
  assertion naming it so a walk that stops reaching it fails rather than
  passing green.
- `git push --force` is no longer a way to unsay something.

Nothing before 0.10.2 becomes reachable; that history stays private, and the
0.10.2 squash is the boundary commit.

`docs/RELEASING.md` §"The two repositories" is the record: what the procedure
lost (a second checkout, a tree replacement, a blob-hash verification, a
withheld-file rule, and the gap between what was gated and what shipped), and
the one rule that stayed with a new reason — a test may not assume the history
it runs against, because the release matrix unpacks a `git archive` with no
`.git` at all.

## Also

`docs/extensions.md` said extension commands are "silent last-write-wins".
They have been first-wins since 0.10.2, with the loser keeping
`ext:<extension>.<command>`. Corrected.
