# Tectum must switch to `--no-builtin-tools`

Status: **open, not applied.** Written 2026-08-20 in the τ repo because τ's
change caused it. The `tectum` repo was deliberately left untouched — applying
this is the Tectum owner's call.

## What broke

τ's `--no-tools` and `--no-builtin-tools` used to be the same thing. `-nbt`
degenerated to `--no-tools`: both dropped the built-in tool set, and both left
extension-registered tools in place. Branch `rename/tau-llm` makes them distinct:

| Flag | resolved `no_tools` | built-ins | extension tools |
|---|---|---|---|
| `--no-builtin-tools` / `-nbt` | `"builtin"` | dropped | **kept** |
| `--no-tools` / `-nt` | `"all"` | dropped | **dropped** |

Tectum passes `--no-tools` and registers its entire capability through
extensions, so it now gets **zero tools**. `speak` is among them, and `speak` is
the terminal verb — without it a τ node cannot produce output at all.

Measured directly against `AgentSession._build_turn_tools` with one
extension-registered `speak` tool:

```
no_tools=None        -> tools offered to the model: ['speak']
no_tools='builtin'   -> tools offered to the model: ['speak']
no_tools='all'       -> tools offered to the model: []
```

## Why this is live rather than hypothetical

`tectum/tau_node.py` resolves τ from `$TAU_BIN`, else `tau` on `PATH`. On the
development machine that is `agent-harness-py/venv/bin/tau`, an editable install
of this repo — so the behaviour follows whichever branch is checked out. It does
not wait for a release.

The change is **not** on `master` and has not been published, so no installed
release carries it yet.

## Tectum's own comments already ask for `-nbt`

The intent is not ambiguous. `tectum/tau_node.py:172-173` reads:

```python
# The extensions ARE the capability (module docstring).
"--no-tools",
```

and the module docstring is headed "**Why the agent has no builtin tools.**"
That is `--no-builtin-tools` described in words. The flag was correct only
because the two used to be the same.

## The change

Six sites, all in the `tectum` repo except the last.

1. **`tectum/tau_node.py:173`** — the fix itself.

   ```python
   -            # The extensions ARE the capability (module docstring).
   -            "--no-tools",
   +            # The extensions ARE the capability (module docstring), so this is
   +            # -nbt and not --no-tools: drop the built-in set, KEEP the
   +            # extension-registered verbs. The two flags were interchangeable
   +            # until tau's rename/tau-llm made --no-tools mean zero tools of
   +            # any kind, extension-registered included.
   +            "--no-builtin-tools",
   ```

2. **`tectum/tau_node.py:18`** — the docstring paragraph "Every τ node here runs
   ``--no-tools``" becomes ``--no-builtin-tools``. Its next sentence ("The
   extensions register the tools, with real schemas") is what makes the new flag
   the right one, and needs no change.

3. **`tests/test_tau_backend.py:217`** —

   ```python
   -    assert "--no-tools" in args and "--no-extensions" in args
   +    assert "--no-builtin-tools" in args and "--no-extensions" in args
   ```

   The test name, `test_the_agent_has_no_builtin_tools_and_no_discovered_extensions`,
   already says `builtin`. Its docstring's "`--no-tools` removes `bash`" wants the
   same substitution.

4. **`tectum/tau_ext/handset_bus.py:33`** — "the owning node runs τ with
   ``--no-tools``, so ``bash`` is not merely discouraged, it is absent." Still
   true of `-nbt`; update the flag name.

5. **`praxis/edge_asr.yaml:16`** — "NO BASH, NO SHIMS. τ runs with `--no-tools`
   and one loaded extension". This text is a system-prompt overlay the model
   reads, so leaving it stale means the model is told about a flag its process
   was not started with.

6. **`ffwfrobotics.github.io/docs/integrations/tectum-tau.md`** — three prose
   mentions (lines ~73, ~91, ~219) plus the flag list inside the SVG figure's
   `<desc>` at line 20. **Change this only after the code changes**, so the page
   keeps describing what Tectum does rather than what it ought to do.

`tests/test_memory_reflex.py:329`
(`test_the_sub_agent_gets_no_tools`) was checked and needs nothing: it asserts a
property of the sub-agent, not the flag string.

## Not a reason to revisit the τ side

The split is pi-faithful (`main.ts:424-428` collapses the two argv booleans into
one resolved value exactly this way), it is what `docs/COMMAND_LINE.md` and the
published CLI table now describe, and `--no-tools` meaning "no tools" is the
reading its name supports. Tectum wanted `-nbt` and there was no way to say so
until now.
