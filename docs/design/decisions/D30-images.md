# D30: Images in tool results (first slice)

**Status:** accepted, building (0.17). **Amends** D12 (the store's `VERSION` and an image directory),
D15 (the walk's turns carry images; a provider declares `image_limits`), D21 (`PROTOCOL` 2, a new
wire shape of `Outcome.Ok`), D24 (`base_tools`' `read` returns images), D26 §7.3 (the image ceiling).
Investigation: akb #563. John's decisions (2026-11-24) are marked **fixed**; the rest are #563's
recommendations, marked **provisional** until he rules on them.

## Context

Nothing in τ 0.16 carries an image: a tool returns a string, `Outcome::Ok(String)` holds it, the
log stores it, a provider sends it as text, and `read` refuses a binary file. A model that can see
(John's local Qwen3.8-27B on llama.cpp; Claude; Gemini) cannot be shown a screenshot, a plot or a
diagram a tool made. This ADR is the smallest slice that gets one image from disk to a model end to
end, and the shapes the later slices extend.

## Decision

### 1. Target and scope (fixed)

τ 0.17, with `PROTOCOL` going from 1 to 2. The first slice is **`read` returns image content**: a
file whose bytes are a PNG, JPEG, GIF or WebP image comes back as an image, through the core value,
the store, the walk, the providers and John's local vision model. Images live **only in tool
results** (`Outcome.Ok`) for now (**provisional**): user images (`@file`, `send(images=)`, the
edge's input), extension function tools returning images, an MCP client and resizing are later
slices (§10).

### 2. The value: `Image`, and `Outcome.Ok(value, images)`

`tau.Image` (core `tree::Image`) is one image: `mime` (`image/png`, `image/jpeg`, `image/gif`,
`image/webp`), `sha256` (lowercase hex of the bytes: its identity), `width` and `height` in pixels,
and `data`, the bytes, when this process has them (below). `Image(data)` sniffs the format and size
from the bytes' header and hashes them; bytes that are not one of the four formats raise
`BadImage` (a `ValueError`). Two images are equal when their `mime`, `sha256`, `width` and `height`
are: `data` is not part of the value.

`Outcome.Ok` gains `images`, a list, empty by default: `Outcome.Ok("text")` is what it was, and
`Outcome.Ok("[image ...]", [Image(png)])` is a result the model reads as text followed by the
images. Only `Ok` carries images: Anthropic refuses an image in an error result, and an error is
words for the model.

### 3. The bytes: next to the log, re-read at send (provisional)

A session in a directory keeps each image once, as `images/<sha256>` beside `log` and `scratchpad`
(content-addressed; **provisional**, #563 D-B). The log holds the reference (`mime`, `sha256`,
`width`, `height`), never the bytes, so the log stays small and a later elision of old images
rewrites nothing.

- **Writing.** Core carries an image's bytes with the value (`data`, never serialized and not
  compared), so a node with a new image reaches the store whole, through the one path every
  write takes (D11). `Store::save` writes each image of a `Write` record whose file is missing, by
  rename after a sync, before the record goes to the log: a log never names an image its directory
  lacks. A node may also carry an image without its bytes (a compaction or prune copy of a node read
  back from disk); the write is refused with `MissingImage` when the session has no such image, so a
  bad reference never reaches the log.
- **Reading.** A loaded node's images carry no bytes. `Tree.walk(node, images=True)` reads them
  from the session's directory (`MissingImage` if a file is gone); the driver samples with it, so a
  provider is always handed images with `data`. A session in memory keeps the bytes in its nodes;
  a read copy (`Session.read`) reads them from the directory it was read from; a fork copies the
  walk's images into its new directory.
- **The wire** carries the reference only (§8). A call that fetches an image's bytes as base64 for
  a client to draw is the next slice's, with tau-code's rendering.

### 4. The store's version (provisional)

`VERSION` goes to 2, and the reader takes 1 and 2 (**provisional**, #563 D-F). A v1 log opens and
replays unchanged (its records are a subset of v2's: `Ok` is still a string when it has no
images). The store that takes a v1 log's lease rewrites its header to 2 before it writes, so a 0.16
build then refuses the session by name (`session file version 2; this build reads 1`) rather than
failing on the first image record. A read copy writes nothing and leaves the header as it is.

### 5. `read`, the reference example (fixed)

`read` stays in the removable `base_tools` extension and is **the** example of a tool that gives a
model an image: sniff, check, return `Outcome.Ok(text, [Image(bytes)])`, nothing more.

- The format is judged by the file's first bytes, not its name. An image is read whole; `offset`
  or `limit` with an image is an `Err` (they count lines).
- The text is one line, `[image <path>: <mime> <W>x<H>, <N> bytes]`, so a model that is shown it as
  a transcript knows what it was given.
- **No resizing in 0.17 (fixed).** An image larger than what the model's provider takes is an
  `Err` that says so: the size, the limit, and that τ does not resize (make a smaller copy with an
  image tool and read that). The limit check is the driver's (§6), so it applies to every tool;
  `read` itself refuses only a file over `max_image_bytes` (default 20 MiB, the most any built-in
  provider takes), so it never loads a file no provider takes.

### 6. Which models take images (provisional)

A model takes images only if its config says `images: true` (`ModelConfig.images`, default
`false`; **provisional**, #563 D-D). A provider says what its API takes as `image_limits`
(`tau.ImageLimits(max_bytes, max_side)`, an optional attribute of the `Provider` protocol, as
`render` is). Built in:

| provider | `max_bytes` (raw) | `max_side` | source |
|---|---|---|---|
| OpenAI-compatible | 20 MiB | none | OpenAI's per-image limit; llama.cpp has none of its own |
| Anthropic | 3.75 MiB | 8000 px | 5 MB per image, read as the base64 size (conservative) |
| Gemini | 15 MiB | none | 20 MB inline request, as base64 |

Two checks, one rule (`tau._images`), and neither is silent:

- **At the call.** When a tool answers `Ok` with images, the driver checks them against the batch's
  model: a model without `images`, a provider without `image_limits`, or an image over the limits
  makes the call's outcome `Err(why)`. The model reads why and goes on; the branch stays sendable.
- **At send (fail early).** A walk holding an image is refused before any request when the model
  does not take images or an image is over its provider's limits (a branch moved to another model,
  a node copied in): the sample fails with `SampleError` naming the node and the model.

### 7. Providers

`turns(walk)` gives each `CallResult`, and each `UserTurn` made from results, its `images`.

- **OpenAI-compatible** (llama.cpp mtmd, vLLM, OpenAI): a `tool` message is text only, so it stays
  text, and the batch's images follow in one `user` message: a text item naming the calls
  (`[images from tool results: <id>]`) and one `image_url` item per image, a `data:` URL in base64.
- **Anthropic:** a `tool_result`'s `content` becomes a list: the text block, then one `image` block
  per image (`source: {type: base64, media_type, data}`). Images come only from `Ok`, so never in an
  `is_error` result.
- **Gemini:** the batch's `functionResponse` parts, then one `inlineData {mimeType, data}` part per
  image, in the same `user` content.

Wherever bytes travel as text they are base64 (fixed); binary transfer is not offered.

### 8. The wire: `PROTOCOL` 2

`Outcome.Ok` without images is encoded as before (`{"Ok": "text"}`); with images it is
`{"Ok": {"value": "text", "images": [{"mime", "sha256", "width", "height"}]}}`. The log and the
wire share this encoding (D21 §4). The schema gains `Image` and the object branch of `Ok`; a client
generated from the 0.16 schema meets an unknown shape, so `PROTOCOL` is 2 and a 1 is refused at
`hello` (`ProtocolMismatch`). tau-code regenerates from the new schema.

### 9. What else meets an image

| where | rule |
|---|---|
| **Counting** (D26) | base64 is never in the byte ceiling; an image's ceiling is `image_ceiling(w, h)`, the most the published formulas give for its pixels without a resize: `max(⌈w/28⌉·⌈h/28⌉, 85 + 170·tiles)` (Anthropic and Qwen-VL's 28-pixel patches; OpenAI's 512-pixel tiles after its fit to 2048 and 768). `lowest_tokens` adds nothing. |
| **Prune** | only the newest turn keeps its images: turn N-1's results become `ok` (no images), turns before are transcript text, and turn N is copied with its images. |
| **Compaction** | the summarizer is sent the head with its images (so it can describe them); the tail is copied by reference and keeps them; the head's images are gone with the head. |
| **TUI** | a tool result shows one line per image, `[image WxH mime]`, under its text. |
| **`tau export`** | the same line in the Markdown; no bytes. |
| **ratatui example** | its strict serde reads both shapes of `Ok` and prints the line. |

## Alternatives

- **Inline base64 in the log** (#563 D-B's other option): one change smaller now, but the log grows
  by every image and the elision slice would have to rewrite it. Rejected (provisional).
- **Leave `VERSION` at 1** (#563 D-F's other option): an old build would fail on the first image
  record with `Undecodable` instead of by name. Rejected (provisional).
- **Pi-style placeholder text for a model without vision** (#563 D-D's other option): the model
  would act on an image it never saw. Rejected for failing early (provisional).
- **A tool keyword passing the limits to `read`** (`call(args_raw, *, images=None)`, as `progress`
  and `call_id` are passed): `read` could refuse before loading, with the exact limit. Rejected for
  the driver's check at the call: one rule for every tool, no new public keyword, and a model change
  between the call and the send is still caught at send.
- **Resizing in the harness** (#563 D-E): needs an image library; deferred by John (fixed).
- **A new `Outcome` variant** instead of a field on `Ok`: every `case Outcome.Ok()` (prune, usage,
  extensions) would miss image results. Rejected.

## Later slices (#563 S2)

User images (`@file`, `send(images=)`, the edge's input); extension function tools returning
`Image`; a `Client` call that returns an image's bytes (base64) and tau-code drawing them; history
elision of old images; harness-side resizing; the OpenAI Responses adapter; an MCP client.

## Glossary additions

`Image`, `BadImage`, `MissingImage`, `Outcome.Ok.images`, image directory, `ImageLimits`,
`Provider.image_limits`, `ModelConfig.images`, `max_image_bytes`, image ceiling; `VERSION` 2 and
`PROTOCOL` 2 in their entries.
