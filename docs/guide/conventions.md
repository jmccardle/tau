# Conventions a client may rely on

τ's protocol carries the tree, the cursors and a few events, and no more than a client cannot work
out for itself. Where a client can work something out from what the tree already holds, τ writes
down the shape and keeps to it, and does not add a call. These are those shapes: documented, not
protocol. A convention is as stable as the protocol is (`PROTOCOL` 3): a change to one is a change
for every client, so it is a release note.

Each is named by an id (`CV-NN`) that the design notes and the release notes use.

| id | what | read |
|---|---|---|
| CV-02 | what a reply cost: `Assistant.meta.usage`, and `/usage json` for money | [Usage](#cv-02-usage) |
| CV-04 | which cursor a sub-agent belongs to: its label | [Sub-agent parentage](#cv-04-sub-agent-parentage) |

## CV-02: usage

Every reply a provider sampled carries what the server said it used, in `Body.Assistant.meta`:
a JSON object held as text, in the field `usage`. A client may read it from each node it holds and
add the nodes up, with no call to τ. Money is the exception: prices are the server's
configuration, so a client that wants cost asks `/usage json`.

### The shape of `meta.usage`

`meta` is text; parsed, it is an object with the reply's facts, among them:

```json
{
  "usage": { "input": 12300, "output": 812, "cached": 11800, "written": 0, "reasoning": 140 },
  "usage_raw": { "input_tokens": 500, "output_tokens": 812, "cache_read_input_tokens": 11800 }
}
```

`usage` is the normalized report, and the same for every provider. Every value is a non-negative
integer, a count of tokens:

| key | always there | meaning |
|---|---|---|
| `input` | yes | every prompt token the request held, cached and written ones included |
| `output` | yes | every token the model produced, thinking included |
| `cached` | no | the part of `input` the server read from its prompt cache. Absent: the server did not say. `0`: it said, or (Gemini) left it out, and it read nothing |
| `written` | no | the part of `input` the server processed and stored in the cache |
| `written_1h` | no | the part of `written` kept for an hour (Anthropic) |
| `reasoning` | no | the part of `output` that was thinking |
| `tool_prompt` | no | the part of `input` a server-side tool's use added (Gemini) |

A key that is absent was not reported; do not read it as `0`. The parts never exceed their whole:
`cached + written <= input`, `written_1h <= written`, `tool_prompt <= input`. τ drops the whole
`usage` of a reply that breaks this, or that sent a malformed count, rather than hand a client a
number that cannot be right. `usage_raw` is what the server sent, as it sent it, kept for a person
to read; its shape is the provider's and no client should depend on it.

A reply with no `usage` key reported none. That is a fact to show ("no usage"), not a zero: a reply
that was aborted, or that a server sent without counts, still used something.

### Adding replies up

A client that sums `usage` over nodes must count the same replies `/usage` counts, or its total
will differ. A node counts when all three hold:

- its body is an `Assistant` and its `by` is `Provider` (a reply an extension wrote in place of a
  sample was never billed);
- `copied_from` and `derived_from` are both unset (compaction and prune write copies of replies
  that are already counted; the copy is not a second bill);
- it has a `usage`.

Replies in the tree that are not on the cursor's walk count too: a branch you left, a summary or
a sub-agent spent real tokens. To total a session, add every node, not the walk.

```python skip
import json


def totals(nodes):
    """Tokens in and out, over every reply the session paid for."""
    tokens_in = tokens_out = 0
    for node in nodes:
        body = node.body
        if not isinstance(body, tau.Body.Assistant) or body.meta is None:
            continue
        if node.copied_from is not None or node.derived_from is not None:
            continue
        if not isinstance(node.by, tau.Author.Provider):
            continue
        usage = json.loads(body.meta).get("usage")
        if usage is not None:
            tokens_in += usage["input"]
            tokens_out += usage["output"]
    return tokens_in, tokens_out
```

### Money: `/usage json`

Cost needs the model's price, which is configuration only the server has (`models.<name>.usage.price`),
and a reply is priced by the model its branch was on at that node. A client asks for it with the
command `usage` and its `format` argument, which takes `text` (the default: the table a person
reads) or `json`: `client.run(cursor, "usage", {"format": "json"})`, or `/usage json` typed. The
result is a JSON object as text:

```json
{
  "models": [
    {
      "model": "anthropic:sonnet", "configured": true, "priced": true,
      "samples": 12, "input": 210000, "output": 9800,
      "cached": 191000, "cached_input": 210000, "written": 4100,
      "cost": "1.270000", "silent": 0, "aborted": 0,
      "estimates": 0, "estimated_cost": null
    },
    {
      "model": "local:qwen", "configured": true, "priced": false,
      "samples": 3, "input": 36900, "output": 2400,
      "cached": 0, "cached_input": 0, "written": 0,
      "cost": null, "silent": 0, "aborted": 0,
      "estimates": 0, "estimated_cost": null
    }
  ],
  "total": {
    "configured": true, "priced": true, "samples": 15, "input": 246900, "output": 12200,
    "cached": 191000, "cached_input": 210000, "written": 4100,
    "cost": "1.270000", "silent": 0, "aborted": 0, "estimates": 0, "estimated_cost": null
  },
  "cost_complete": false
}
```

- `models` has a row for every model that has replied, in the order each first replied, and
  `total` is their sum (it has no `model`).
- `samples` counts replies that reported usage, and `input`, `output`, `cached` and `written` add
  their `meta.usage`. `cached_input` is the `input` of those replies that said whether they read the
  cache, so `cached / cached_input` is the hit rate, and is not skewed by a server that never says.
- `cost` is dollars as an exact decimal string (never a float, never scientific notation), or
  `null` for a model with no price: `null` is "unknown", never "free". `cost_complete` is false
  when a model that replied has no price, so the `total.cost` leaves it out (the text form ends that
  total with `+`).
- `silent` counts replies that reported no usage and `aborted` those cut off before they did.
  τ estimates what they cost, apart from `cost`: `estimates` is how many and `estimated_cost` their
  guessed price, or `null` when nothing is estimated. `/usage` and the spending limit count the
  estimate; `cost` does not.
- Cost is worked out when you ask, from the prices in the config the session was started with.

The figures are those of the text form: `/usage` and `/usage json` read the same rows.

## CV-04: sub-agent parentage

A client that draws the cursors of a session may group a sub-agent under the cursor that started
it. The label says which. τ's `agents` extension labels each cursor it opens

```text
agent:P:N
```

where `P` is the id of the cursor whose `agent` call opened it, in decimal, and `N` counts that
cursor's agents from 1, so the first agent the main cursor (id 0) starts is `agent:0:1`, the next
`agent:0:2`, and one started by a branch on cursor 2 is `agent:2:1`. Read it with the pattern
`^agent:(\d+):(\d+)$`; anything that starts with `agent:` and does not match is not τ's.

- **Where a client sees it.** Labels are in the session snapshot's `Labels` event and in each
  later one, as `(label, cursor)` pairs. A cursor with a label of this shape is a sub-agent of
  cursor `P`, whether or not `P` is still open and whether or not the agent is: an agent is
  closed once it has answered, and its label stays on the closed cursor as its restart record.
  `Labels` is in the snapshot, so a client that attaches late groups them too.
- **Depth is one.** An agent is not offered the `agent` tool, so a sub-agent has no sub-agents of
  its own and no label has two parents.
- **Continuing.** The model continues an agent with `to="agent:P:N"`. The label does not change, and
  neither does `P`: the parent is the cursor that first started the agent.
- **The name the model reads.** The result of a call starts `agent:P:N answered:`, so the same label
  appears in the tree's `Results` node, and a client can match a result to its cursor.
- **Other labels.** Other extensions label their cursors for their own use (`compaction:1`, a
  character, a search): none of them is a sub-agent, and none starts with `agent:`.

The parent is not in the tree as a node, and not on `CursorView`: a client that wants the group
reads the label, and a client that does not draws the cursors flat, as before.
