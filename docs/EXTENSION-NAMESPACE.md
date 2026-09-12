# Two names for every extension command

Built 2026-09-11. An extension command now has a name nothing can take from it,
and a name anyone can contest. Shadowing stops being a fight over one slot and
becomes a binding a human can settle.

## 1. What was wrong

`ExtensionRegistry._commands` was one flat unattributed `dict[str, dict]`, last
write wins. Measured at `0683595`, with two extensions each registering `/note`
and A loading first:

| Gesture | What happened |
|---|---|
| load A, then B | B answers `/note`; both buckets claim it |
| disable A — the SHADOWED one | `/note` is gone, though B is still enabled |
| disable B — the winner | `/note` is gone; A's did not come back |
| reload A | A silently took `/note` back from an enabled B |

Rows 2 and 4 are one cause: `_unregister_bucket` removed the names its bucket
had *registered*, and the registry had no record of which extension a slot
actually belonged to. `ExtensionHandlers`' own docstring named the gap — the
bucket was "the one place that records *which* extension registered *what*" —
and nothing joined it back to the slot.

Row 3 is the shape of the data, not a bug in the unregister path: a dict slot
holds one value, so A's command was destroyed the moment B registered.

A fifth fault sat beside them. `ExtensionAPI.register_flow` called
`register_command` and then wrote `_flows[name]` separately, and
`register_command` never touched `_flows` — so B shadowing a flow-declared
`/note` inherited A's `FlowDeclaration`. Every head rendered A's argument form,
validated against A's domain, and dispatched the result into B's handler. Fully
initializable and silently wrong, which is worse than broken.

## 2. Two names, three tables

`ext:pirate.speak` is the pirate extension's `speak`. It is minted by
`ExtensionAPI.register_command` from the extension's own identity, the extension
never spells one itself, and only two files sharing a stem can collide on one.

| Table | Key | Contested? |
|---|---|---|
| private registry (`_commands`) | `ext:<extension>.<command>` | no |
| bindings (`_bindings`) | typed name → qualified name | **yes** |
| flows (`_flows`) | `ext:<extension>.<command>` | no |

`register_command` does two things: it installs the command in the private
registry, where nothing can displace it, and it claims the typed name. The
return value says whether the claim succeeded — `None` when `/speak` now runs
this command, otherwise the qualified name that holds it instead.

Keying flows by the qualified name is what closes §1's fifth fault by
construction: B's binding of `/speak` cannot reach a flow declared under a name B
does not hold. `Vocabulary.aliases` carries the bindings so a typed name still
finds its flow — one `Flow` object, two ways in.

## 3. First-wins, and why it reverses the old rule

An unpinned typed name goes to whoever asks first. Last-wins was never "the
newer registration is the more intentional one": load order is discovery in
`sorted(iterdir())` order followed by explicit `-e` paths, so it handed the name
to whichever file sorted later, and renaming a file silently changed what
`/speak` did.

The reversal is only defensible because the loser keeps its qualified name. Under
the old model first-wins would have meant the second extension's command was
unreachable; here it means one of its two names is taken.

**A disable does not restore a predecessor.** There is no stack. Disabling the
extension that holds `/speak` leaves `/speak` unbound rather than falling back to
whoever asked second — that extension is at its own qualified name, and a human
can pin the typed name to it. The alternative is a per-name stack in the core,
which was considered and refused: it makes the core guess at an ordering the
extensions never stated.

## 4. Composition by name, not by handout

The first design for this handed the displaced command *object* to whoever
displaced it, with a matching `on_unregistered` callback so an incumbent could
fight back. It is not built, because a qualified name does the same work with
less machinery and one property the handout cannot have: **late binding**. A
captured callable outlives the registry — disable its extension and the wrapper
still calls a dead extension's handler with nothing saying so. `api.run_command`
resolves at call time, so a disabled extension is a name that stops answering.

```python
def register(api):
    async def note(args, ctx):
        prior = await api.run_command("ext:a_ext.note", args)
        return f"B wraps [{prior}]"
    held_by = api.register_command("note", {"description": "…", "handler": note})
    # held_by is "ext:a_ext.note": A got the typed name, B is at ext:b_ext.note.
```

`api.get_command(name)` reads what a name resolves to, so an arriving extension
can decide whether to claim a typed name at all. `api.unregister_command(name)`
withdraws one of this extension's own, and raises on anything else — the typed
name is shared, the command is not.

## 5. Pins

A pin is a human's choice of what a typed name means, and it beats every
registration. It is a preference rather than session state, so it belongs in
`~/.tau/config.json` beside the per-extension slices, not on a session.

Pinning a target that is not loaded leaves the typed name **unbound** rather than
bound to nothing: the pinned extension claims it whenever it registers, and no
other extension takes it in the meantime. That makes the outcome independent of
load order, which is the point. A pin also survives `unregister_command`, so
disabling and re-enabling an extension restores what the human chose.

## 6. What each surface shows

`AgentSession.get_extension_commands` lists the TYPED names — what the palette
and the completion popup offer. `get_qualified_commands` lists the private
registry. The RPC `get_commands` result ships both, with a new `hidden` boolean
marking the qualified half (`protocol_version` 1.6, additive).

`resolve_command` is handed `registry.command_names()`, the union, so
`/ext:pirate.speak` runs whether or not `/speak` is bound to it.
`ExtensionInfo.commands` — what `/extensions` renders — now holds qualified
names, so the listing says what an extension OWNS rather than what it asked for.

## 7. The grammar

`ext:<owner>.<command>`. `parse_command` validates nothing and splits the line on
its first space, so this needed no parser change.

`split_qualified_command` splits on the LAST `.`, so an owner stem containing one
(`my.ext.py`) still parses; `qualified_command` refuses a command name containing
a `.`, which is the half τ controls.

`extension_owner` normalizes the bucket's path label into the owner token: a file
extension's stem, an inline factory's own name (its label is `module:qualname`),
then reduced to word characters. That label was documented as "not load-bearing";
it is now, because a derivative of it is typed after a `/`.

## 8. Not in this pass

- **The `/ext:` completion grammar.** Typing `ext:` should complete extension
  names and then their commands. `complete_command` is a pure prefix match today
  and the two-stage form is a special case inside it.
- **The binding UI and the config key.** `pin_command` exists and nothing calls
  it: no `"commands": {…}` block is read from `config.json`, and no head offers a
  way to re-point a typed name. Until both land, a contested name is settled by
  load order and the loser is reached by typing its qualified name.
- **A registration log.** Which extension claimed what, and which was refused, is
  visible only by reading `get_bindings()` against `get_commands()`. A recorded
  log was designed alongside the handout in §4 and is worth building once
  something has to be diagnosed.
- **Tools.** `register_tool` still raises on a duplicate, so a tool has one name
  and cannot be wrapped. Whether tools get the same namespace is a separate
  decision, deliberately not taken here.
