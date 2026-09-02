# The system prompt survives a fold

**Status: built (2026-08-30).** One rule changed in one function
(`ConversationTree._active_path_entries`): a splice carries the system messages out
of the span it drops. This document is why that was a defect rather than a design,
what it was hiding, and the four consequences.

---

## 1. What was observed

Eliding a span removed the system prompt from the conversation, which is not what
an elide is for and not what its own row said it would do. Reproduced on an
in-memory session with a system prompt and eight messages:

```
before: [system, u0, a0, u1, a1, u2, a2, u3, a3]
after elide(first_kept=u2): [u2, a2, u3, a3]
```

It was never elide-specific. `compaction` and `elide` are both in
`_SPLICE_ANCHOR_KINDS` and share one fold, which drops **everything on the path
before `firstKeptId`** — and in τ the system prompt is on that path.

---

## 2. Why τ has this case and pi does not

pi keeps the system prompt in the request frame. It is not a session entry, it is
not on any path, and `buildSessionContext` cannot drop it because it never sees it.

τ diverged: `Session._init_state` writes the prompt as the first `message` entry,
with the stated reason "uniform reconstruction" — one kind of thing on disk, and a
session that can be rebuilt from its own log. That is the right call for the
tree-as-truth model, and it is what put the prompt inside the splice's reach.

So this is not a port fault. It is the second-order cost of a deliberate
divergence, and it was paid quietly for as long as it existed.

---

## 3. Why nothing appeared to be broken

The model kept receiving a system prompt. `AgentLoop._call_llm` re-inserts
`self.config.system_prompt` at index 0 whenever the context does not already start
with a system message:

```python
if _first_role != "system":
    messages.insert(0, {"role": "system", "content": system_prompt})
```

That line was written for a different case — a backend whose conversation history
has no system message at all — and it silently absorbed this one. The visible
result was a TUI that stopped showing the system box after a compaction, with the
model apparently unaffected.

**What was actually happening is worse than a missing prompt.** After a fold, the
prompt on the wire came from `config`, not from the tree. For a session running
under its own creation-time config those two strings are the same, so nothing
diverged in practice. For a session resumed after the config's `system_prompt` was
edited, or run by a frontend whose `_build_system_prompt` composes different
context files, they are not the same string — and the fold said one thing while the
request contained another. That is the invariant τ's whole session model rests on:
what the model saw is the path, and nothing else.

---

## 4. The change

`_active_path_entries` collects the system messages in the span it is about to drop
and emits them **before the anchor**:

```python
carried, kept = [], []
found = False
for entry in path[:anchor_idx]:
    if entry["id"] == boundary:
        found = True
    if found:
        kept.append(entry)
    elif is_system_message(entry):
        carried.append(entry)
return [*carried, anchor, *kept, *path[anchor_idx + 1 :]]
```

Before the anchor, because a system message has to be first for the provider, and
because that is where `AgentSession.compact_messages` already puts it — its manual
path has always returned `[*system_msgs, summary_msg, *kept]`. The two agreed about
system messages at the message level and disagreed at the entry level; now they do
not.

`is_system_message` is `type == "message"` and `message.role == "system"`. A
`customMessage` is deliberately not included: an extension-injected node carries
`role: "custom"` and is remapped to `user` at the wire, so it is ordinary
conversation and a splice is entitled to fold it away.

### Four consequences, all of them intended

1. **A compaction keeps it too.** Same fold, same carry. `find_valid_cut_points`
   never chose a system message as a cut point (only `user` and `assistant`
   qualify), so every compaction was dropping it.
2. **The loop stops substituting.** With the fold's first message a system message,
   `_call_llm`'s insert does not fire, and the prompt on the wire is the one in the
   tree.
3. **Span counts got smaller by one.** A row saying "folds 3 entries" over a span
   whose first entry is still visible was describing a different operation from the
   one it performed, so `_splice_span_phrase` and `TauBackend.elide_span` both
   exclude system messages from the count. The visible effect is that an elide
   which would only drop the system prompt now correctly refuses as a no-op.
4. **A system message inside the KEPT region is not duplicated.** The carry takes
   only what the splice drops.

---

## 5. What it cost

Fourteen test expectations changed, and each is the same statement: the folded
context now begins with the system prompt. The one worth naming is the fold-parity
battery, which compares `ConversationTree.context_for` against the frozen System-A
oracle (`SessionManager._build_active_path` + `get_active_messages`) across four
synthetic trees at every leaf.

That battery could have been narrowed to the uncompacted trees. It was not.
`_expected_from_oracle` states the divergence as a transformation OF the oracle's
output — restore the system messages the splice dropped, at the front — so every
other message and every ordering is still pinned against System A byte for byte.
`test_the_only_divergence_from_the_oracle_is_the_carried_system_message` asserts
that the transformation is EMPTY on the trees and leaves where no splice applies,
which is what stops a helper written to absorb one difference from absorbing the
next one silently.

Three tests were also given an extra message, because a fixture whose whole dropped
span was the system prompt now folds nothing and stops testing what it was named
for.

---

## 6. What this does not do

- **It does not make the system prompt uneditable or pinned.** It is an ordinary
  entry on the path; a fold carries it, and nothing else about it changed.
- **It does not touch `_call_llm`'s insert.** That line still covers its original
  case — a context with no system message anywhere — and is now unreachable for a
  session that recorded one.
- **It does not deduplicate several system messages.** A log with three of them on
  one path carries all three, in path order. That log is already unusual, and
  inventing a "first one wins" rule here would be a second answer to a question
  the fold has never had to ask.
- **It does not change what `compact_messages` sends the summarizer.** For the
  first compaction on a path, `prepare_compaction` has always included the system
  entry in `messages_to_summarize`; for later ones its `boundary_start` sits after
  the carried entries, so they are not re-summarized. That asymmetry predates this
  change and is left alone.

---

## 7. Where to look

- `tau-agent-core/src/tau_agent_core/conversation_tree.py` — `is_system_message`,
  the carry in `_active_path_entries`, the count in `_splice_span_phrase`.
- `tau-agent-core/tests/test_conversation_tree.py` — the parity battery and the
  three carry tests (`test_elide_carries_the_system_prompt`,
  `test_a_system_message_below_the_boundary_is_not_duplicated`,
  `test_a_custom_message_is_not_carried`).
- `docs/TREE-BROWSER-AS-EDITOR.md` §6 — the branch mode that depends on this: "keep
  only the system prompt" would have produced a context with no system prompt in it.
- `docs/TREE-EDITOR-MANUAL.md` §9 — what a reader is told about it.
