# ffwf-tau-jmfts

A JMFTS-backed session store for **Tau**, a programmable coding agent harness,
plus the agent tools that search it.

Tau stores a conversation as a tree of entries. JMFTS — a retrieval appliance
combining matryoshka embeddings, ColBERT-style late interaction, and BM25 hybrid
search over PostgreSQL with pgvector — stores a corpus as a tree of documents.
This package is the observation that these are the same shape: point
`tau --store jmfts` at a JMFTS server and every entry in the session **is** a
document, with the same id, the same parent links, and the same search machinery
as anything else in the corpus. Memory is not a feature bolted onto the agent;
the agent's history is simply kept somewhere that can already be searched.

## Why it is a separate package

Because it is optional, and staying optional is the point. `ffwf-tau-coding-agent`
does not depend on it, and `tau_coding_agent` never imports `tau_jmfts` at module
scope — the store is resolved lazily, only when `--store jmfts` (or the
equivalent config key) selects it. A default Tau install has no JMFTS in it at
all. The dependency arrow points `tau-jmfts → tau-agent-core`, never
`tau-coding-agent → tau-jmfts`.

## Install

```bash
pip install ffwf-tau-jmfts
```

Python 3.11 or newer. Pulls in `ffwf-tau-agent-core`. If you are installing it
for use with the `tau` command, `pip install 'ffwf-tau-coding-agent[jmfts]'` is
the same thing spelled as an extra.

You also need a running JMFTS server — see
<https://github.com/jmccardle/jmfts>.

## Use it as a session store

```jsonc
// ~/.tau/config.json
{
  "session_store": {
    "backend": "jmfts",
    "url": "http://localhost:8100",   // or $JMFTS_API_URL
    "token": null,                    // or $JMFTS_API_TOKEN
    "parent_id": null,      // optional: host document for new conversation roots
    "index": "tau"          // optional: BM25 index to register roots into
  }
}
```

```bash
tau --store jmfts                          # per-run override
tau --export-session REF out.jsonl         # JMFTS subtree → JSONL, then exit
tau --import-session out.jsonl             # JSONL → JMFTS subtree, then exit
```

Each session becomes one `tau:conversation` root document, and each entry — user
message, assistant message, tool call, compaction summary, branch marker —
becomes one child with a `tau:` usetype. The entry payload lives in
`structured_content`; the document's `content` is a plain-text projection so
search has something to match. Writes pass `auto_embed=False`, so a
conversational turn never waits on a GPU forward pass.

Three things fall out of the shared shape rather than being implemented:

- **Fork is subtree copy.** Branching a conversation is the same operation as
  copying any document subtree, with cross-referencing entry fields remapped
  onto the new ids.
- **Scope is `parent_id`.** JMFTS's subtree filter is, unchanged, "only hits in
  this conversation".
- **A session survives its file.** The file store and the JMFTS store are
  interchangeable views of the same tree, and `--import-session` /
  `--export-session` move between them losslessly.

## Give the agent recall

Two extensions ship in `tau_jmfts.ext`. τ loads extensions by file path — pass
one to `-e`, or drop a copy into the `~/.tau/extensions` discovery directory:

```bash
tau -e "$(python -c 'import tau_jmfts.ext.tools as m; print(m.__file__)')"
```

- **`tools.py`** registers `jmfts_search`, `jmfts_read`, and `jmfts_ingest`.
  **Recall is a tool call, not an injection.** There is no hook that quietly
  prepends "relevant memories" to the prompt: when the agent wants to remember,
  it calls `jmfts_search`, and that call and its results become real `toolCall`
  and `toolResult` entries on the session path — persisted, visible in the
  transcript, forkable, and subject to compaction like everything else. You can
  read a session later and see exactly what the agent recalled and when.
- **`enrich.py`** runs on `session_shutdown`: it embeds every substantive entry
  (chunking long ones first) and indexes the conversation root into a BM25
  index. τ's write path never embeds, so this is the deferred half of that
  bargain. Both steps are idempotent and resumable from server state, so a pass
  that crashes halfway can simply run again.

The tools work regardless of which session store is active — a file-backed
session can still search JMFTS. Only the `scope="conversation"` shorthand needs
a JMFTS-backed session, and it says so rather than quietly searching everything.

## Use the client directly

```python
from tau_jmfts import JmftsClient, JmftsSessionLog

with JmftsClient("http://localhost:8100", token="...") as client:
    log = JmftsSessionLog.create(client, cwd=".", model="gpt-4o", backend="openai")
    for entry in log.entries():
        print(entry["type"])
```

`JmftsClient` is a thin synchronous `httpx` wrapper that raises `JmftsError` on
any non-2xx response. `JmftsSessionCatalog` is the discovery side —
list, resolve a ref, create, load, fork, delete. `import_session` and
`export_session` are the JSONL round-trip.

## What the seam refuses to do

Failures at this boundary are handled by refusal, not repair. A memory system
that silently degrades is worse than one that stops.

- A `--store jmfts` run with a missing URL, a bad token, or an unreachable
  server exits with an error at startup. There is no fall-back to file storage.
- `--session-dir` combined with `--store jmfts` is a hard error, not a guess
  about which one you meant.
- `load()` rejects a root that is not a well-formed `tau:conversation`, and
  raises if the entry sequence shows a second writer touched the tree.
- Foreign documents filed under a conversation are tolerated and surfaced, but
  can never move the session's cursor — an out-of-band write cannot redirect
  where the next turn lands.
- Forking raises on an unresolvable cross-reference rather than copying a
  dangling anchor.
- `jmfts_ingest` refuses any `usetype` beginning with `tau:`. That namespace
  belongs to the store, and an agent must not be able to forge conversation
  entries into its own history.
- `--no-session` uses an ephemeral in-memory log that never touches the server.

## Docs

- `docs/JMFTS-INTEGRATION-PLAN.md` — the design, and its delivery status.
- `docs/SESSION-TREE-IMPLEMENTATION.md` — the entry algebra this store backs.

Repository: <https://github.com/jmccardle/tau>

## The rest of Tau

| Distribution | Imports as | What it is |
|---|---|---|
| `ffwf-tau-llm` | `tau_llm` | the provider and streaming layer |
| `ffwf-tau-agent-core` | `tau_agent_core` | the agent loop, tools, sessions, extensions |
| `ffwf-tau-coding-agent` | `tau_coding_agent` | the `tau` command and the Textual TUI |

MIT © Fight Fire with Fire Robotics, LLC
