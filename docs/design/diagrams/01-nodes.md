# 01 Nodes and bodies

Three things are stored: the **node** and the **move record**, which are
written once, and the **scratchpad item**, which is not part of the tree. A node
is written once and never changed (I2). Its `body` is exactly one of six
kinds. Four kinds are **message-bearing** and reach the model; `Config` and
`Custom` are bookkeeping and never do (Q3). An `Assistant` body is an ordered
list of **parts**, stored as they streamed; a `Call` is a part, never a node of
its own.

Labels in these diagrams (`u1`, `a1`, `r5`, `c3`) stand for session-local
integer ids (Q8). `c3` is a call id, `r5` is the result that answers `c5`.

## The node

```
┌─ Node ─ immutable, written once (I2) ───────────────────────────────────
│ id            session-local integer, assigned at append (Q8)
│ parent?       absent only on a root; every root is a Config (V2, Q5)
│ time          supplied by the driver; core reads no clock
│ by            frontend(client) | provider | tools | core | extension(name)  (Q7)
│ body          exactly one of the six kinds below
│ copied_from   a list of nodes whose content this repeats verbatim, in order
│               (new parent; empty for most nodes)
│ derived_from  a list of nodes whose walks this content was computed from
│               (empty for most nodes)
│ delivers      a list of scratchpad item ids this node consumed
│               (empty for most nodes; ids, never content)
└─────────────────────────────────────────────────────────────────────────
```

## The six body kinds

```
body
├─ System      { text }                                   message-bearing
├─ User        { text, images }                           message-bearing
├─ Assistant   { parts, stop, stop_raw? }                 message-bearing
│     parts, in the order they streamed
│     ├─ Text(text)
│     ├─ Thinking(text, signature?)
│     └─ Call { id, tool, args_raw }       a part, not a node; args_raw is
│                                          the model's text, never re-serialised
├─ Result      { call, outcome }                          message-bearing
│     outcome
│     ├─ Ok(value)
│     ├─ Err(error)
│     ├─ Skipped(reason)
│     └─ Interrupted
├─ Config      { snapshot }                               bookkeeping
└─ Custom      { kind, data }                             bookkeeping
      known kinds: sample_error (core)
```

| kind | reaches the model | typical `by` |
|---|---|---|
| `System` | yes | core (`Session.create`) |
| `User` | yes | frontend(client) (a delivered input), or extension(name) (a rewritten input, a summary or reminder, Q30) |
| `Assistant` | yes | provider (extension(name) if a hook answered or rewrote it) |
| `Result` | yes | tools (core for `Interrupted`, `Err("invalid arguments")`, `Err("hook error")`) |
| `Config` | no | core (the session's first root, I6 restatement); frontend or extension(name) when explicit; extension(name) for a root a hook creates |
| `Custom` | no | core (`sample_error`), extension(name) |

`stop` and `stop_raw`:

```
stop     end        the model chose to stop
         tools      handed off to tool execution      the only stop that owes results
         length     output limit reached
         refused    provider filter or refusal
         interrupted  we cancelled it (abort)
         error      stream failed after output had arrived
stop_raw the provider's own string (end_turn, tool_use, ...), for debugging
```

## One instance of each shape

```
k0  Config     by core        { snapshot: model big, tools, hooks, max_steps ... }     root
└ s   System     by core      { "You are ..." }        Session.create writes k0 and s
  └ u1  User       by frontend  { "fix the bug", images: [] }
    └ a1  Assistant  by provider
        parts : [ Text "I'll look", Thinking "...", Call{c3, read, '{"p":"a.rs"}'},
                  Call{c5, grep, '{"q":"fn m'} ]            <- args_raw may be malformed
        stop  : tools          stop_raw: tool_use
        (a batch: c3 and c5)
      └ r5  Result     by tools     { call: c5, outcome: Ok(...) }
        └ r3  Result     by tools     { call: c3, outcome: Ok(...) }

e1  Custom     by core        { kind: sample_error, data: ... }   a node like any other,
                              parented under a context tip; never reaches the model
```

A `Result` names its call by id; the `Call` part is on `walk(r5.parent)` (I1).
`copied_from` and `derived_from` appear only on nodes a hook wrote (see
`05-compaction.md`); `delivers` appears on a delivered input
(`04-uncommitted-and-detached-roots.md`). They record a relation, and are never
a parent.

## The other stored objects

```
Move { cursor, from?, to, by, time, opened_by?, label? }
        │      │      │   │              │           └ opening moves only: a unique, fixed name (Q33)
        │      │      │   │              └ names a cursor, not a node (Q6)
        │      │      │   └ frontend(client) | core | extension(name) | tools  (Q28)
        │      │      └ the node the cursor lands on; this becomes `at`
        │      └ absent on an opening move (Q5)
        └ cursor id: a session-local integer, assigned by the opening move
Cursor = { id, at }      at = the `to` of the cursor's latest move

Item { id, from: { frontend?, extension, cursor }, to, data }      in the scratchpad, off the tree
        │         │                                 │   └ any JSON; its meaning is the extensions'
        │         │                                 └ the cursor it is addressed to; adding wakes it
        │         └ who added it, and the client whose input started it
        └ a session-local integer, never reused
anyone may add, update, or remove an item; load drops an item some node delivers
```

Reads with: `model.md` §1 (stored objects), §3 (committed and uncommitted are
derived, not stored), §4 I2 (immutability), I4 (a `stop == tools` reply owes
results), Q3, Q7, Q8, Q27; `glossary.md` terms node, body, part, stop, call,
result, outcome, author, copied_from, delivers, scratchpad, item.
