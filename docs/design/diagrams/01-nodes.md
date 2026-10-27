# 01 Nodes and bodies

Four things are stored. The **node**, the **move record** and the **label
record** are written once and never changed. The **scratchpad item** sits off
the tree and may be updated and removed. A node's `body` is exactly one of six
kinds. Four kinds are **message-bearing** and reach the model; `Config` and
`Custom` are bookkeeping and never do (Q3). An `Assistant` body is an ordered
list of **parts**, stored as they streamed; a `Call` is a part, never a node of
its own. A batch is answered by one `Results` node with one outcome per call.

Labels in these diagrams (`u1`, `a1`, `r1`, `c3`) stand for session-local
integer ids (Q8). `c3` is a call id, `r1` is the `Results` node that answers a
batch.

## The node

```
┌─ Node ─ immutable, written once (I2) ───────────────────────────────────
│ id            session-local integer, assigned at append (Q8)
│ parent?       absent only on a root; every root is a Config (V1, Q5)
│ time          supplied by the driver; core reads no clock
│ by            frontend(client) | provider | tools | core | extension(name)  (Q7)
│ body          exactly one of the six kinds below
│ copied_from   a list of nodes whose content this repeats verbatim, in order
│               (new parent; empty for most nodes)
│ derived_from  a list of nodes whose walks this content was computed from
│               (empty for most nodes)
│ delivers      a list of scratchpad item ids this node consumed: inputs on a User
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
├─ Results     { outcomes }                               message-bearing
│     outcomes: [ { call, outcome } ], one per call, in call order (V2: distinct calls)
│     outcome
│     ├─ Ok(value)
│     ├─ Err(error)                        a tool's error, or a hook's string (its reason)
│     └─ Interrupted
├─ Config      { snapshot }                               bookkeeping
└─ Custom      { kind, data }                             bookkeeping
      known kinds: sample_error (core)
```

| kind | reaches the model | typical `by` |
|---|---|---|
| `System` | yes | core (`Session.create`) |
| `User` | yes | frontend(client) (a delivered input), or extension(name) (a summary, a reminder, a task, Q30) |
| `Assistant` | yes | provider (extension(name) if a hook answered or rewrote it) |
| `Results` | yes, expanded into one tool message per call (§10) | tools (extension(name) if a hook answered or rewrote an outcome; core if every outcome is core-written) |
| `Config` | no | core (the session's first root); frontend or extension(name) when appended on purpose |
| `Custom` | no | core (`sample_error`), extension(name) |

`stop` and `stop_raw`:

```
stop     end          the model chose to stop
         tools        handed off to tool execution      the only stop that runs calls
         length       output limit reached
         refused      provider filter or refusal
         interrupted  we cancelled it (abort)
         error        stream failed after output had arrived
stop_raw the provider's own string (end_turn, tool_use, ...), for debugging
```

## One instance of each shape

```
k0  Config     by core        { snapshot: model big, tools, hooks, max_steps ... }     root
└ s   System     by core      { "You are ..." }        Session.create writes k0 and s
  └ u1  User       by frontend(laptop)  { "fix the bug", images: [] }   delivers: [4]
    └ a1  Assistant  by provider
        parts : [ Text "I'll look", Thinking "...", Call{c3, read, '{"p":"a.rs"}'},
                  Call{c5, grep, '{"q":"fn m'} ]            <- args_raw may be malformed
        stop  : tools          stop_raw: tool_use
        (a batch: c3 and c5)
      └ r1  Results    by tools     { outcomes: [ {c3, Ok(...)},
                                                  {c5, Err("invalid arguments")} ] }
                                    written at the join; outcomes were in memory until then

e1  Custom     by core        { kind: sample_error, data: ... }   a side node under the
                              cursor's at; on no cursor's walk; never reaches the model
```

`c5`'s outcome is core-written: its `args_raw` did not parse, so the tool
never ran. `copied_from` and `derived_from` appear only on nodes a hook
wrote (see `05-compaction.md`). They record a relation, and are never a
parent.

## The other stored objects

```
Move { cursor, from?, to, by, time, opened_by? }
        │      │      │   │         └ names a cursor, not a node (Q6)
        │      │      │   └ frontend(client) | core | extension(name) | tools  (Q28)
        │      │      └ the node the cursor lands on; this becomes `at`
        │      └ absent on an opening move (Q5)
        └ cursor id: a session-local integer, assigned by the opening move (Q33)
Cursor = { id, at }      at = the `to` of the cursor's latest move

Label { label, cursor? }           a unique string; cursor absent = the label is removed
   "research-agent-3ca7"               ──▶ 4
   "Web Search Agent (Rust Patterns)"  ──▶ 4      a cursor may have many labels;
   "reviewer"                          ──▶ 7      a label names one cursor at a time
   setting a label another cursor has moves it; load replays the records

Item { id, from: { frontend?, extension?, cursor }, to, data }      in the scratchpad, off the tree
        │         │                                  │   └ shape set by the kind below
        │         │                                  └ the cursor it is addressed to; adding wakes it
        │         └ the cursor that added it, and who
        └ a session-local integer, never reused

  from has         kind         data                         consumed by
  ─────────────────────────────────────────────────────────────────────────────────────
  frontend client  input        { text, images }             the input phase: a User node delivers it
  otherwise        extension's  any JSON                     whoever reads it; a node may deliver it

anyone may add, update, or remove an item; load drops an item some node delivers
```

Reads with: `model.md` §1 (stored objects, labels, items), §2 (batches and
results), §3 (derived views), §4 I2, I4, I5 (V1, V2), Q3, Q7,
Q8, Q27, Q33; `glossary.md` terms node, body, part, stop, call, results,
outcome, author, copied_from, delivers, label, scratchpad, item.
