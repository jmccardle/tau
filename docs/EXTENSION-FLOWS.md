# Extension flows: an extension saying what its command takes

**Built 2026-09-05.** `api.register_flow` lets an extension declare a command's
argument in the same vocabulary τ's own gestures use, so the command gets tab
completion, a rendered form and a palette argument in every head — the Textual TUI
and an RPC client alike — with no head code written for it.

This document records what was added, the two properties the design rests on, what
it costs, and what it deliberately does not do.

---

## 1. The problem

`api.register_command("done", {"description": …, "handler": …})` gives a command a
name and a handler and says nothing else. Every head could therefore show the name
and hand the handler whatever was typed, because nothing anywhere said what should
have been typed.

The consequences were visible in three places at once:

| Surface | Built-in `/model` | Extension `/done` |
|---|---|---|
| editor | completes model names | completes the word `/done`, then nothing |
| bare command | opens a picker | runs the handler with `""` |
| palette | asks for the argument | asks for a free-text line, or nothing |

`docs/TUI-STYLE-GUIDE.md` §3.1 and §6 already named this: **an extension cannot
declare a flow** — `FLOWS` was a frozen tuple and `ExtensionAPI` had no
`register_flow` — so an extension drove `ui.form` itself and forwent partial
binding, completion and a palette entry.

## 2. What an extension writes

```python
from tau_agent_core.capabilities import Argument, Domain

def register(api):
    api.register_flow(
        "done",
        "mark a todo done",
        handler,                                   # (args, ctx) -> str, as before
        argument=Argument("item", "todo_item", "Which todo."),
        domain=Domain("todo_item", "An open todo.",
                      enumerator="extension", field_kind="select"),
        values=lambda query, limit: [...],         # -> [(value, label)]
    )
```

`register_flow` registers the command too, so this replaces `register_command`
rather than accompanying it. **The handler contract does not change**: it is still
called `(args, ctx)` with the bound value as text, so an extension that adds a
declaration to a command it already shipped keeps working for every caller that
never learned about the declaration.

An argument whose domain τ already declares needs no `domain` and no `values` —
`Argument("name", "model_name", "Which model.")` is enough, and the models come
from τ's own enumerator.

## 3. The two properties the design rests on

### 3.1 The registry stays frozen; an extension arrives as a layer

`FLOWS`, `DOMAINS`, `CAPABILITIES` and `VIEW_COMMANDS` are still module constants.
`Vocabulary` is one frozen value over them, `BUILTIN` is the instance holding τ's
own, and every pure function that reads the registry takes one, defaulting to
`BUILTIN`:

```python
next_step(flow, bound, cursor, vocabulary=BUILTIN)
flow_arguments(flow, vocabulary=BUILTIN)
flow_form_spec(flow, bound, *, options, vocabulary=BUILTIN)
bind_command_args(flow, raw, vocabulary=BUILTIN)
bind_text(argument, text, vocabulary=BUILTIN)
enumerate_domain(domain, *, …, vocabulary=BUILTIN)
dispatch_builtin(name, args, *, cursor, vocabulary=BUILTIN)
complete_command_argument(text, vocabulary=BUILTIN)
```

`AgentSession.vocabulary` is `BUILTIN.extended_with(…)` over its own registry's
declarations, cached against a `flows_revision` counter because building it runs
the whole cross-check and a head asks on every keystroke.

**A mutable global was the alternative and it is wrong here.** A fork, a
`switch_session` and a sub-agent are separate `AgentSession`s in one process, so a
global would have let one session see another's flows — and `next_step` would have
stopped being the pure function `resolve_command`'s docstring says it is: "a head
peeking, a runtime deciding, and a test with neither must all get the same answer
from the same function." The default argument is what keeps every existing call
site working unchanged; the parameter is what keeps two sessions apart.

`test_extension_flows.py::test_two_sessions_do_not_see_each_other` is that property
as a test, and `test_a_session_with_no_flows_pays_nothing` holds that a session
with no extensions returns the `BUILTIN` object itself.

### 3.2 An overlay is checked by the same rules as τ's own tables

`_check_registry` used to read the four module globals directly. It now takes them
as parameters and runs over the combined tables every time `extended_with` builds
an overlay. An extension therefore cannot declare something a built-in could not:
an argument naming an undeclared domain, a domain with an enumerator nothing
answers, a flow whose arguments its mutation does not take.

The one piece that had to be invented is what an extension's *mutation* is.
`_check_flow_matches_mutation` requires a declared `Capability`, and an extension
has none — its mutation is its handler. `extended_with` synthesises one per added
flow (`on_wire=False`, arguments taken from the flow, `returns` the one
`{"output": str|null}` shape every extension command already reports). That is why
the overlay can be validated by the same function rather than a second, laxer one.

Two rules are checked at **registration** rather than when the vocabulary is next
built, because a registration should fail where it was made: a name τ already
declares, and an enumerated domain with no `values` callable.

### 3.3 Why the enumerator is held apart from the domain

`Domain` is a declarative record that goes over the wire; a Python callable cannot.
So `Vocabulary.domains` carries the domain and `Vocabulary.enumerators` carries the
callable, keyed by domain name. A head reads the domain from `next_step` and calls
`enumerate_domain`; only the process holding the extension ever runs the callable.

This is also why `AgentSession.vocabulary` is `NOT_EXPOSED` in the RPC capability
audit: publishing the object itself would put a callable in a JSON result.

## 4. What a head has to do: nothing

`tau-coding-agent/tests/test_extension_flow_tui.py` registers a command this
repository has never heard of and drives the TUI's own surfaces. `app.py` contains
no mention of `done` or `todo_item`. What works:

- `/do` completes to `/done` with its description
- `/done ` lists `Buy milk`, `Ship the release`, `Book flights`
- `/done ship` narrows to one
- Tab inserts the VALUE, not the label — `/done t1 `
- `/done` with no argument is a `FlowStep`, so the head renders the form
- a bound flow reaches the handler

The one head change is four lines in `TauApp._perform_ready`: a flow in
`vocabulary.extension_flows` is performed by `_dispatch_extension_command` rather
than by looking for a backend method named after it. Everything else was already
generic and simply needed the vocabulary passed.

## 5. On the wire

`next_step` and `enumerate_domain` are already RPC verbs, so a remote head drives
an extension flow over the shipped protocol with no new verb. Both handlers now
read `handler.session.vocabulary` rather than the module globals — that one-line
change is what makes extension flows reachable from tau-code.

`get_commands` gains a **`flow: bool`** field per row, so a host can tell which
names it can step without calling `next_step` on all of them. Every built-in flow
is `true`, the two view commands are `false`, and an extension command is `true`
only if it used `register_flow`. The field is additive; a host that ignores it
behaves as before.

## 6. What this deliberately does not do

- **One argument at most.** Refused at registration, not truncated. An extension
  flow ends in a handler taking one typed line, and splitting one line across two
  arguments has no rule — the same refusal `bind_command_args` already makes for
  built-ins. A gesture needing several fields drives `ui.form` itself, which is
  what it did before this existed.

- **No new capability on the wire.** An extension's mutation is not an RPC verb and
  its synthesised `Capability` carries `on_wire=False`. Making an extension able to
  publish a verb is a different feature with a different blast radius: the verb
  table is a shipped contract and `get_capabilities` is how a host version-negotiates.

- **No `register_view`.** Views are head code (`capabilities.py`'s own position), so
  an extension declaring one would only force every head to pretend it had it.

- **`/extensions <verb>` still does not complete its verb.** The three words are not
  a `Domain`. They could now become one — this is the machinery that would let them
  — but the routing table `EXTENSION_VIEW_VERBS`/`_EXTENSION_FLOWS` is still derived
  twice, once in the core and once in `app.py`, and collapsing that is its own
  change. See docs/SLASH-COMMANDS.md §6.

- **A second extension registering the same command still wins silently.**
  `register_command` overwrites where `register_tool` refuses, and
  `register_flow` inherits that. It is pre-existing and was not changed here,
  because changing it changes command semantics for extensions that never used a
  flow.

- **No cardinality `"many"`.** `Argument` can say it and nothing here rejects it,
  but the one-argument limit and the text handler contract make it meaningless for
  an extension flow today.

## 7. Where the pieces are

| Piece | Where |
|---|---|
| the layered registry | `tau_agent_core/capabilities.py` → `Vocabulary`, `BUILTIN`, `FlowDeclaration` |
| the cross-check, now over parameters | `capabilities.py` → `_check_registry` |
| the author-facing call | `tau_agent_core/extension_types.py` → `ExtensionAPI.register_flow` |
| storage, and the revision counter | `tau_agent_core/extensions/registry.py` |
| the session's own registry | `tau_agent_core/agent_session.py` → `AgentSession.vocabulary`, `_run_extension_flow` |
| the head's read | `tau_coding_agent/app.py` → `TauApp._vocabulary` |
| the wire | `tau_agent_core/rpc/commands.py` → `next_step`, `enumerate_domain`, `get_commands` |
| tests | `tau-agent-core/tests/test_extension_flows.py`, `tau-coding-agent/tests/test_extension_flow_tui.py` |
