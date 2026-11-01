# router: friction log

Files: `examples/speculative/router.py` (112 lines, about 50 of them logic),
`tests/examples/speculative/test_router.py` (12 tests, all pass). Line numbers below are
`router.py` unless a test is named. The tests use four fake models on one `FakeProvider`
(`main`, `cheap`, `think`, `coder`, each with its own model id, read back from
`provider.requests[i].model.model`); the Harness allows that without friction.

## Does

A `pre_input` hook reads the input draft. If it starts with `!name ` and `name` is a configured
model, the branch goes to that model and the prefix is stripped from the text the model sees.
Otherwise the first regex of `Options.when` that matches picks the model in `Options.use`
(`list[Model]`, so `/config` checks the names against the catalog). A switch writes a `Config`
node (`cursor.set_model`) and a `Custom` node of kind `route` saying why, both before the `User`
node, and only when the branch is not on that model already; the typist also gets a `Notice`.
`/route on|off` (or `/config router enabled=false`) stops the table, not the prefix. With
`once=True` a `post_reply` hook puts the branch back on the last model not set by the router,
after the reply that ends the turn (not after a tool-call reply).

## Easy

- 91-101: `pre_input` has exactly what the job needs: the text (`draft.body`), the author
  (`draft.by`) and a draft it may replace (98). One hook both strips and routes, and the change
  applies to this very sample because `Config` is written before the `User` node commits
  (`test_each_input_is_routed...` shows the full walk: Config, Custom, User, Assistant).
- 83: `cursor.set_model(Model(...))` is checked before it writes and returns after moving the
  cursor; no `Config` body to build.
- 23-53: the table is an option; `__post_init__` makes a bad regex or unequal lists a `ConfigError`
  at `/config` time, and the `Model` type refuses `gpt-9` there too
  (`test_the_table_is_checked_when_it_is_configured`).
- 67-70: `/route off` is three lines; `Literal["on", "off"]` gives the form and the completion; the
  built-in `/config router enabled=false` does the same job without it.
- 81: the "write only on change" check keeps the tree to one `Config` per switch (6 `Config`
  nodes in the 6-input test, 1 root plus 5 changes).
- 103-112 (`once`): the restore target is derived from the tree (the last `Config` not authored by
  the router), no memory, so it survives a reload (`test_routing_survives_a_reload_because_it_is_in_the_tree`).

## Fought

### (a) Expressing the behaviour

1. **No way to read the current model.** Intent: "am I on it already?" and "what was it before I
   routed?". API: a handle reads nodes; `options()` is folded, the model is not. Did: parse every
   `Config` node's JSON `data` myself (56-64), i.e. depend on the wire format. Severity: awkward.
   Every frontend that wants to show the model faces the same fold.
2. **No catalog on a handle.** `!think` has to be tried: `set_model` raises `ConfigError` for a
   non-model (82-85). A typo (`!thnk hello`) therefore goes to the model as text, which is a
   quiet failure; a Fail-Early router would refuse it, but cannot tell it from `!important:` text
   without a list of models (`test_a_prefix_forces_a_model_and_is_not_sent` pins the pass-through).
   Severity: awkward.
3. **A table is two lists.** The type language has no mapping or pair (25-42). Equal length is
   checked by hand. Also a list option cannot default to anything but `None` (same as
   bash_guard), so the router does nothing until configured. And `/config` splits a list on commas,
   so a regex such as `^.{0,24}$` cannot be typed there (I wrote short-chat as a lookahead,
   `^(?!.{25})`, to keep the test table comma-free). Severity: awkward.
4. **Two inputs, one sample.** Routing is per `pre_input` item, a sample is per step. Two pending
   inputs are routed one after the other; the first route's nodes are written and immediately
   superseded (`test_two_inputs_in_one_step_are_routed_each_but_sampled_once`: a `think` Config
   and a `cheap` Config, one sample on `cheap`). Severity: cosmetic here (rare), but it shows
   "the model for this turn" is not something core has.
5. **A later gate leaves the router's nodes behind.** The hook writes before it knows the input is
   delivered. If an extension listed after the router refuses the input, the item is removed but the
   `Config` and `Custom` nodes stay with no `User` node after them
   (`test_a_later_gate_that_refuses_the_input_leaves_the_routers_nodes_behind`). The alternative
   is `pre_sample`, which I tried in a scratch run: it sees the committed `User` node
   (`cursor.node()`), `set_model` there also changes this sample, and gates have already run; but it
   cannot strip the prefix, the walk becomes User, Config, Assistant, and it must not override an
   explicit prefix, which needs memory or a look at the walk. Severity: awkward.
6. **One-shot is possible but "turn" is the extension's guess.** `once` restores in `post_reply`
   when `stop != Tools` (108). An abort in the tool phase writes no reply, so no restore
   (`test_an_abort_in_the_tool_phase_leaves_the_branch_on_the_routed_model`); with a table that has
   a catch-all the next input corrects it. Severity: cosmetic.
7. **Who owns the model.** With a catch-all rule the router overrides a `/model` the user typed at
   the next input; `enabled=false` is the way out. Design, not API; by reading, not tested.

### (b) Showing state to the user

1. **Which model answered, and why.** The tree holds the switch (`Config` by `Extension("router")`)
   and, by my convention, a `Custom` `route` node with the reason (86). The answer itself carries
   the provider's model id in `Assistant.meta` for the real providers (`providers/openai.py:174`,
   `anthropic.py:165`), not the catalog name; `FakeProvider` copies nothing there unless the script
   does. Nothing in core links a reply to a catalog name. Severity: awkward.
2. **`tell` has no recipient in `pre_input`.** `cursor.client` is the nearest earlier frontend
   `User` node's author: none on the first input (a bare `cursor.tell(...)` raises `ValueError`,
   `test_the_first_input_has_no_previous_typist_to_tell`), the previous typist later. Did: read
   `draft.by.client` and pass `to=` (88, 96). Severity: awkward (a trap: it works from the second
   input on, for one client).
3. **A `Notice` is for the typist, now.** Another client, or a reload, sees only the nodes. The
   `route` node is only meaningful to a frontend that knows the convention; `examples/chat.py`
   prints nothing for it. Severity: cosmetic.
4. **The typed text is not recorded.** The `User` node holds the stripped text; `!think` survives
   only as `why: "prefix"` in the `Custom` node. Severity: cosmetic.

### (c) Getting input from the user

Nothing is asked; the prefix is the inline channel and needed no API. The one wish is the
fail-early refusal of a bad `!name` (see (a)2), for which `pre_input` could return a string (the
item is removed and the typist told) if only it could tell a model from text.

### (d) Timing and concurrency

1. **Order inside the step is clear and sufficient.** `pre_input` runs before the input commits and
   before the sample starts, so writing `Config` there is the "before this sample" hook (I1). Verified
   by the provider's recorded model. `pre_sample` works too (see (a)5).
2. **The `/route` command is a step.** It is `Busy` unless the cursor rests and it writes a
   `Config` node of its own (options). Fine for `/route`; a command that must act while a step runs
   has to be a plain command with no `Cursor`, and then it cannot write the branch's options.
   Cosmetic.
3. **Nodes accumulate by design.** Each switch is two nodes (Config, Custom); `once` adds a third
   on the way back. For a chatty session that flips models every input, that is three nodes per turn.
   Subtraction option: drop the `Custom` node and keep the reason in the `Notice` only.

## Frontend view

A chat line client shows `[router: think (prefix)]` to the typist, lists `/route <mode>` under `/`,
completes `on|off`, and answers `[routing off]` to the command; it shows no model name anywhere.

A TUI or web frontend that wants "model: think" in its status line has to fold the walk's `Config`
nodes itself (the same code as `model_on`, 56-64), since `CursorView` carries no model, and has to
treat `Custom` nodes of kind `route` as inline dividers by convention. It also needs `Appended`
(arrives for every client, `Config` and `Custom` before the `User`) to know when the model changed
(`test_the_typist_is_told_and_every_client_sees_the_nodes`), and `Client.options(cursor, "router")`
for the table and `enabled`.

## Since stage D

`cursor.model()` and `cursor.models()` exist, and `CursorView.model` carries the folded model to every
client. `switch` asks `cursor.models()` instead of trying `set_model` and catching `ConfigError`, and
compares with `cursor.model()` instead of folding the walk. `model_on` stays for the one thing the
handle cannot say: the model the walk had before the router's own `Config` nodes.

## Wish

Smallest first.

1. **`CursorView.model` (or `Client.model(cursor)` and `Cursor.model()`):** the model name the
   next sample resolves to, folded by the same code as the sample. Deletes `model_on` for the
   "already there?" use (56-64, 81, 110), and every frontend's fold. No new concept: it is a
   read of what `config_at` and the layers already decide.
2. **In `pre_input`, `cursor.client` is the draft's author.** The draft being processed says who
   typed it. Deletes `who` and `to=` (78, 88, 96) and the trap in (b)2.
3. **`cursor.models() -> list[str]` (the catalog's qualified names).** Replaces the try/except at
   82-85 with a lookup, and lets the router decide explicitly what to do with a `!name` that is
   not a model (refuse it, or pass it on).
4. **List defaults (shared with bash_guard)** so a router can ship a default table, and the
   JSON-array form for `/config` (`when='["^.{0,24}$"]'`).
5. **Fine as is, extension's job:** the two-list table (no record type; a mapping would be a new
   concept), the `Custom` `route` node (a convention), `once` (a 9-line hook). The honest
   alternative to all three is to leave the router small: table plus prefix, no `once`, `tell`
   only, and let `/model` be the one-shot override.
