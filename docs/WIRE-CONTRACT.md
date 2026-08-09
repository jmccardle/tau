# The wire contract: what actually crosses project boundaries

Four projects have to agree: **tectum** (voice in, dispatch), **τ** (the agent), **JMFTS**
(memory/retrieval), **McRogueFace** (the world). This file is the whole shared surface.

Everything here is transcribed from code that runs. Where this file and `SIM_SPEC_v2.md` disagree,
the spec is the one that's wrong — it describes behaviours and rationales, never the data.

**Verified 2026-07-29** against `~/Development/tectum` (`tectum/event.py`, `tectum/subjects.py`,
`tectum/tools.py`, `tectum/nodes/effectors/*`, `parley-nats/parley_nats/bridge.py`) and exercised
end to end: τ took a turn off the bus and its `…out.speak` was acked by a real `parley-nats`.

---

## 0. The envelope

**Every** message on the tectum bus is one `TectumEvent`, serialized as JSON
(`tectum/event.py:47`). There is no bare-payload form. `TectumEvent.from_dict` indexes eight keys
without a default, so omitting any one of them is a `KeyError` in the consumer, not a soft failure:

```json
{
  "event_id": "8c1f…",                                  // UUID4 string, required
  "event_type": "events.workspace.responder.out.speak", // == the subject, required
  "source": "agent.responder",                          // producer identity, required
  "timestamp": "2026-07-29T20:46:11.402Z",              // ISO 8601 UTC, required
  "sequence_number": 0,                                 // monotonic per source, required
  "ttl_ms": 60000,                                      // required
  "payload": { "…": "type-specific body" },             // required
  "origin_node": "tau",                                 // required
  "produced_by_schema": null,
  "routed_by_schema": null,
  "binding_id": "ed494428…",
  "expectation": null,
  "residual": null,
  "hops": ["tool.speak"],
  "seen_by": [],
  "audit": { "via": "tau" }
}
```

**`binding_id` is preserved across hops**, not minted per message (`event.py:23`). It correlates
one logical flow — utterance → turn → effector event → ack. Whatever a turn publishes carries the
binding_id of the event that started it. tectum's own shim does this via `TECTUM_BINDING_FILE`
("per-turn binding re-stamp", `tools.py:227`). Getting this wrong doesn't just lose provenance: the
ack subject is built from the binding_id, so a fresh one sends the ack where nobody is listening.

## 1. Inbound — something publishes, τ takes a turn

Which subject drives an agent is **a property of the active praxis schema, not a fixed convention.**
Two shapes exist:

```
events.workspace.<agent>.in                      # subjects.agent_in() — FOUR tokens, no verb
events.sensation.audio.resolved.clean            # what praxis/harness_text.yaml uses
```

The four-token form matters: a subscription to `events.workspace.<agent>.in.>` requires five or
more tokens and will **never fire**.

Payload for the `resolved.clean` form (`parley_nats/bridge.py:73`) — τ reads `payload.text`:

```json
{ "text": "Kevin, can you hear me?", "tagged_text": "…", "words": ["Kevin,", "can", "…"],
  "unclear": [], "overlapping": [], "confidence": 0.99, "duration": 1.5,
  "partials_accumulated": 1, "raw_text": "…", "raw_tagged_text": "…", "corrections": [] }
```

τ: `extensions_builtin/nats_bus.py` — `inbound_subject` is required config, no default.

## 2. Effector event — τ publishes, an effector acts

```
events.workspace.<agent>.out.<verb>              # subjects.agent_out()
```

Known verbs: `speak`, `journal_append`, `jmfts_write`, `delegate` (`tectum/tools.py`).
Payload as tectum's shim builds it (`tools.py:247`):

```json
{ "text": "Loud and clear.", "agent": "responder" }
```

τ: `nats_bus.py` `_make_effector`. Consumers: `tectum/nodes/effectors/*`, and `parley-nats`
renders anything matching `.out.speak`.

## 3. Ack — the effector publishes, τ blocks on it

**Per-effector, three different subject shapes, and the kind token is not always the verb:**

| Verb | Ack subject | Source |
|---|---|---|
| `speak` | `events.action.speech.completed.<binding_id>` | `effectors/speech.py:107` |
| `journal_append` | `events.journal.append.<binding_id>` — `append`, **not** the verb | `effectors/journal_append.py:48` |
| `jmfts_write` | `events.journal.jmfts_write.<binding_id>` | `effectors/jmfts_write.py:61` |
| `delegate` | *none* — the curator acks nothing; its answer arrives later as a separate `posted` event | `tectum/tools.py` |

**There is no `status` field.** An ack is a TectumEvent whose payload is effector-specific.
Failure is signalled by `ok: false` or a non-null `error`; absence of both is success — which is
the only correct reading, since `journal_append`'s ack carries just `doc_id`.

```json
// speech (effectors/speech.py, and parley_nats/bridge.py:103 stands in for it)
{ "text": "Loud and clear.", "backend": "parley-nats", "dsp": "none", "ok": true, "error": null }

// journal_append
{ "doc_id": 1234 }
```

τ subscribes the ack subject **before** publishing (subscribe-first), so a fast effector can't ack
into a subscription that doesn't exist yet.

## 4. Tool schema — JMFTS generates, τ gives to the model

```json
{ "name": "quick_search", "description": "…",
  "parameters": { "q": { "type": "string", "description": "query", "required": true } } }
```

Generated from each `@expose`'d method's Python signature (`ExposeSpec.func` +
`typing.get_type_hints`) — no new registry field needed. Working generator:
`jmfts-needle/catalog/expose_catalog.py` on branch `feat/needle-experimental-program`.

Demo read verbs: `quick_search`, `get_document`, `get_neighbors`. The other 87 ops are on an
explicit exemption list; an op whose parameters can't be typed is unprojectable and refused rather
than exposed with invented parameters.

---

## Open questions

**`move_to` has no consumer and no payload.** McRogueFace has **no bus code at all** — a search of
the checkout finds no NATS client and no `events.workspace` reference outside its copy of the spec.
So `move_to`/`wait`/`note` are spec verbs with nothing on either end. Since τ is currently the only
implementation, τ can define the payload and this file becomes the contract for whoever builds the
world side; there is nobody to wait for.

**Enum-valued tool parameters are stringly typed.** `method`, `direction`, `rerank_method` have
small fixed value sets but are annotated `str`, so the generated schema says "string" and the model
guesses. Fix in JMFTS (`Literal[...]`) or in a τ-side overlay — undecided.

---

## Who implements which side

| Shape | Publisher | Consumer | Status |
|---|---|---|---|
| Inbound utterance | parley-nats / tectum sensors | τ | **working** |
| Effector event | τ | tectum effectors, parley-nats | **working** for `speak` |
| Ack | effectors / parley-nats | τ | **working** for `speak` |
| Tool schema | JMFTS | τ | generator exists; τ-side projection (tau-008) not built |
| World action | τ | McRogueFace | no consumer exists |
