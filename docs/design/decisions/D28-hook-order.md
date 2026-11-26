# D28: Hook order, and usage as a leaf built-in

Status: accepted 2026-11-18 (the owner's decisions A and B, taken before this record was written;
the choices below that the decisions left open are marked *chosen*). Amends D19 §2 (load order is
no longer hook order), D24 §1 (the built-ins' order), §3 (usage's price), M3 and ruling 6 (where a
price lives), and D13 (`ModelConfig` loses `price` and gains `sections`; a model's config may hold
a section per extension).

## Context

Hooks ran in load order (D19 §2): `tau`, then the built-ins sorted by key, the packages, the
user's drop-ins, the project's, `-e`, then `session_name`. At each point every hook ran on one
shared draft, and the first that returned a string halted the rest (`run_hooks`,
`CallRun._hooks`). That order was an accident of where the code came from:

- usage's budget halted `pre_sample` only because `usage` sorts after `compaction`, which had
  already summarized (and paid for it) by then;
- `attachments` had to expand an input before a user extension's `pre_input` read it, which
  held only because built-ins load first (D24 §1);
- a user drop-in `usage.py` that shadows the built-in (D19 §2) moved from row 1 to row 3 and so
  ran after every package, losing its place with no way to get it back.

Separately, `usage` was not a leaf: the meter, compaction, prune and three TUI modules imported
helpers from `tau._usage`, `tau.Usage` was exported, and `ModelConfig.price`, which only usage
reads, was a core field. A replacement usage extension could not replace it.

## Decision

### 1. Order is per hook

Every hook (one extension's hook at one point) has an **order**, an integer, default 0. At each
point the extensions that hook it run sorted by `(order, load position)`. The sort is stable on
load position, so with no order set anywhere the run is exactly the load order, as before. It
applies to every point alike, cursor points and tool points (`pre_tool`, `pre_result`), with no
special case: the halting rule (the first string halts the rest) and the shared draft are
unchanged.

### 2. The extension declares it (*chosen*: an `order` attribute)

An extension declares its defaults in an optional attribute **`order`**: an `int` (every hook it
has) or a mapping from hook point to `int` (those hooks; the others are 0).

```python
class Budget:
    name = "budget"
    order = {"pre_input": -100, "pre_sample": -100, "post_reply": 100}
```

Why an attribute and not a `tau.order(n)` decorator: it has the same shape as the config's
override (§3), so there is one thing to learn; it works for a module that is an extension (C3's
rule) as well as for a class; and it is checked where the other attributes are
(`DefinitionError` naming the extension and its source, at load: a value that is neither, a hook
the extension does not have, an `int` on an extension with no hooks).

### 3. The user's config overrides it

`extensions.NAME.order` is an `int` (all the extension's hooks) or an object `{hook: int}` (those
hooks; the rest keep the declared value). It is laid over the declared value hook by hook.

- It is not an option: it lives beside the options under `extensions.NAME` and every extension
  has it, with options or without. `ModelCatalog.orders` holds it apart from `extensions`.
- It merges across layers like any object (D13): an object over an object merges by hook, an
  `int` replaces. An object over an `int` keeps the `int` for the hooks it does not name, held as
  the key `"*"` (every hook the object does not name; a person may write it too), so `order: 50`
  in the file with `--set NAME.order.pre_sample=-100` leaves the other hooks at 50.
- `order` is reserved under `extensions.NAME`: an options field named `order` is a
  `DefinitionError`. `--set NAME.order=N` and `--set NAME.order.HOOK=N` set it for one run;
  `tau config set` writes it (an object merges by hook into what the file holds).
- *Chosen:* it is read from the config layers only (the files, `TAU_SETTINGS`, `--set`), never
  from a `Config` node: `/config NAME order=...` is refused. Load order is fixed at start, and an
  order that changed from branch to branch would make one tree's hooks run differently per
  branch for no use anyone has asked for. The driver reads it again when its catalog changes
  (`use_catalog`), so a saved change applies from the next job.
- **Fail early.** For a loaded extension, a value that is not an integer, an object whose
  values are not, a hook name that is not one of the extension's hooks, or an `int` for an
  extension with no hooks is an error, in `tau config check` and at every start (the start
  stops), naming the key and the extension's hooks. An order for an extension that is not loaded
  is a warning and ignored, as its options are (an extension may be a deleted file); for one
  switched off by `load`, it is kept quietly. `tau` and `session_name`, which every start has and
  which have no hooks, are errors in both the check and the start.

### 4. Conventional values, and the built-ins' numbers

| value | for | built-ins at it |
|---|---|---|
| −150 | a hook that may rewrite a call, before the guards judge it | `hooks` `pre_tool` (D34: `updatedInput`, so `permissions` rules see the arguments the call runs with) |
| −100 | **guards**: hooks that halt (a budget, a permission) | `usage` `pre_input`, `pre_sample`; `permissions` `pre_tool`; `compaction` `pre_tool` (a summarizer's calls); `hooks` `pre_input` (D34) |
| −50 | an edit that the other edits must see | `attachments` `pre_input` |
| 0 | **edits**: hooks that change the draft, the call or the line (and the default) | `compaction` `pre_sample`, `pre_step`, `post_reply`, `post_step`; `agents` `pre_sample`; `skills` `pre_step`; `step_limit`, every hook |
| +100 | **observers**: hooks that record or show what happened | `usage` `post_reply`; `tokens` `post_reply`; `hooks` `pre_result`, `post_reply`, `post_step` (D34: they see the final result, and after `step_limit` has turned auto-step off) |

Built-ins have no place beyond their numbers: a drop-in or package at the same number runs after
them only because built-ins load first. The reasoning per built-in:

- **usage**: the budget is the reason this record exists; at −100 it halts before compaction
  summarizes, before a user hook answers a sample for free, and before attachments read files.
  Its status line is an observer.
- **permissions**, **compaction**'s `pre_tool`: a refusal before any hook edits or answers the
  call; a later `pre_tool` can still change the outcome (model.md §7: not a sandbox).
- **step_limit**: all hooks at 0. They halt nothing (they count, and `post_step` turns
  auto-step off), so they are not guards; at 0 load position keeps them exactly where they were
  among the built-ins, and before every user extension at 0, so a user `post_step` sees auto-step
  already off and stops cleanly (`examples/speculative/ralph.py` depends on this). A hook that
  turns auto-step back on after it wins over the limit, as before; one that wants to stay bounded
  orders its `post_step` below 0. (Ordering it after the edits was tried: ralph then wrote one
  more iteration after the limit.)
- **attachments** at −50: after the guards (a refused input is never read from disk), before the
  edits, which must see the files attached (D24 §1, now by number rather than by load order).
- **compaction**: its triggers are edits (they move the cursor), after the guards so a budget
  halts before a summary is paid for. Its `pre_step` and `post_reply` only note which reply a step
  committed; they stay at 0 so a halting observer cannot hide a reply from the soft limit.
- **agents**, **skills**: edits. **tokens**: an observer. `base_tools` and `prune` have no hooks.

What changed against the old order (built-ins by key, then user extensions), point by point:

- `pre_input`: usage moved from last (after attachments, step_limit) to first.
- `pre_sample`: usage moved from last (after agents, compaction) to first.
- `post_reply`: usage and tokens now run after every user extension at 0 (before, all built-ins
  ran before any user extension); among the built-ins, compaction, tokens, usage is unchanged.
- `pre_step`, `post_step`, `pre_tool`: unchanged, among the built-ins and against user
  extensions at 0 (attachments' −50 and the guards' −100 keep them ahead of user hooks, where
  load order had them).

### 5. Visibility

- `tau ext list --hooks` prints each point that has a hook, then its extensions in run order with
  their numbers, a configured one marked: `pre_sample  usage -100, agents 0, compaction 0`.
  With `--json`, one object per point.
- `tau config check` reports every bad order (§3), and stays silent when there is none: the
  table is `tau ext list --hooks`'s.

### 6. usage is a leaf built-in

- The **counting rules** move to `tau._counts` and become public: `tau.Reading`,
  `tau.reading(node)`, `tau.counted(node)`. The formatters `tokens` and `show` move there too,
  private. The meter, compaction, prune and the TUI import them from there.
- `tau.Usage` is no longer exported: a program reaches `usage` as a built-in, through
  `load_extensions` (the tests do, through `tests/shipped.py`).
- **Leaf:** nothing in `python/tau` imports `tau._usage` but `_builtins.py`'s `BUILTINS`.
  `scripts/check_rules.py` fails otherwise, so deleting `_usage.py` and its one row there leaves
  a working τ.

### 7. A model's config holds a section per extension (*option a*)

`models.X.NAME` is a **model section**: a JSON object that belongs to the extension named
`NAME`. Core carries it unread, as `ModelConfig.sections[NAME]` (a deep read-only copy); sections
layer and inherit down groups like `options` (objects merge by key, `null` drops).

- An extension that reads one declares **`model_options`**: a frozen dataclass whose fields are
  the section's keys; its `__post_init__` checks and converts the values (`Price.of`). The
  values language (D16) is not used: a section is read by its extension only, never edited by a
  client, and may hold objects and decimal money, which the values language does not have.
- The extension reads it through any handle: **`model_options(name=None)`**, its own section of
  the model `name` (default: the handle's `model()`) built as its `model_options` type; an absent
  section is the type built with no arguments. `ConfigError` if the extension declares none or
  the section does not fit.
- *Chosen (validation):* the same rule as options, at `tau config check` and at every start. A
  section its extension's `model_options` refuses (an unknown key, a missing required one, a bad
  value) is an **error**, and the start stops. A section naming no loaded extension, or one that
  declares no `model_options`, is a **warning**, and is ignored; for an extension switched off by
  `load`, it is kept quietly. This keeps one rule for "the config names something this start does
  not have" (an extension may be a deleted file, or a project's config may name one a teammate
  has not installed), while a typo is still reported, with its path, at every start.
- A key in a model that is not a model key and not an object is still an error (`unknown key`).
  A section naming no loaded extension whose name is a near-miss of a model key (`optons`) is an
  error suggesting the key, not a warning: a misspelt `options` would otherwise drop the
  provider's settings with only a warning.
  An extension whose name is a model key (`model`, `options`, ...) cannot have a section.
- **usage's price** is `models.X.usage.price`, the same four fields (and `cache_write_1h`), USD
  per million tokens (D24 ruling 6 stands). `Price` moves into usage and is no longer public.
  The old place, `models.X.price`, is a `ConfigError` that names the new one; 0.16 is unreleased,
  so there is no migration.
- *Chosen:* **`context_window` stays a core model key.** Two built-ins read it (compaction's
  limits, usage's `ctx`), and so would any counter or prune that measured the window; as a
  section it would be written twice (`compaction.context_window`, `usage.context_window`) or owned
  by one extension that the other then depends on. It describes the model, not one extension's
  view of it, which is the test for a core key.

## Alternatives

| option | why not |
|---|---|
| A decorator `tau.order(n)` on each hook | Two shapes (decorator in code, object in config) for one thing; a module extension's hook functions would need it too. |
| Order per extension only (one number for all its hooks) | usage needs −100 for its budget and +100 for its status line. |
| Order settable per branch (`/config`) | No use; a branch's hooks would run in a different order from its sibling's. |
| Built-ins always first, then user extensions by order | Keeps the positional privilege the decision removes; a shadowing drop-in still loses its place. |
| `models.X.extensions.NAME` for sections | One more level for every section; the owner chose `models.X.NAME`. |
| Error for a section naming no loaded extension | A project config naming a teammate's extension would stop everyone else's start. |
| `context_window` as a section | See §7: two readers. |

## Glossary additions and changes

- New: **hook order** (`order`, `extensions.NAME.order`, `ModelCatalog.orders`), **guard**,
  **observer** (as order bands), **model section**, **`model_options`**,
  **`TreeHandle.model_options`**, **leaf built-in**.
- Changed: **`Reading`**, **`reading`**, **`counted`** (public, in `tau._counts`); **`Usage`**
  (not exported; `ModelOptions`, `price` in its section); **`ModelConfig`** (`sections`, no
  `price`); **`Price`** (private, usage's); **load order** (no longer hook order).
