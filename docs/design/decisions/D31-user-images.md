# D31: Images in user messages (first slice)

**Status:** accepted, built, for 0.18.0 (released 2026-11-28). **Amends** D30 (images leave the tool
results: a `User` node and an `Input` item carry them; the mock provider takes them), D12 (the
store's `VERSION` 3, written only once needed), D21 (`PROTOCOL` 3: new shapes of `User` and
`Input`, `images` on `input` and `send`, the call `image`), D24 §5 (`@path` attaches an image
file as an image), D26 §7.3 (a user's images count as a tool's do). Proposal: akb #613. John's
decisions (2026-11-25, #613's annotation) are marked **fixed**.

## Context

D30 put images in tool results only. A person cannot show a model a screenshot without a tool
reading it first: the web client has no way to attach one, `tau -p` cannot pass one, and `@shot.png`
inlines nothing (the file is not UTF-8, so it becomes a reference). The providers can already send
an image in a `user` message (D30 §7 builds one for a batch's images); what was missing is the
value, the store, the wire and the frontends. This ADR is the tau side of that slice; tau-code's
composer follows on the protocol it fixes.

## Decision

### 1. Target and scope (fixed)

τ 0.18, with `PROTOCOL` going from 2 to 3, and the whole wire surface in 0.18.0: `images` on
`input` and `send`, and the call `image`. A `hello` naming 2 is refused (`ProtocolMismatch`), as
2 refused 1. The slice is: `send(images=)` from any client, `@path` of an image file, the store,
the providers, the counts and the image rule. Not in it: a `tau -p --image` flag (**fixed: no**;
`@path` covers it, and stdin images later), resizing, garbage-collecting unused images, editing
and resending a message with images (a client concern, 0.18.x).

### 2. The value: `Body::User { text, images }`

A user message is its text, then its images (**fixed**: text first, no interleaved parts, until a
fixture shows images-first answers better). `Body::User` gains `images: Vec<Image>` (D30's
`Image`, bytes carried and never serialized). It encodes as before, `{"User": "text"}`, when it has
no images, so a v2 log loads unchanged, and as `{"User": {"text": ..., "images": [...]}}` when it
has some; an object with any other field is refused. `ItemData::Input { by, text, images }` takes
them too, the field left out of the encoding when empty. The input phase moves an input's images
into the draft's body, so a `pre_input` hook sees and may edit `draft.body.images`.

`Body::images()` (Python `Body.images`) is every body's images: a `User`'s, then a `Results`' `Ok`
outcomes' in call order. Everything that met images through D30's four `Results`-only helpers
(`_counts`, `_images.node_images`, `openai._has_image`, the store's `keep_images`) reads this one
instead, so a fork, a compaction copy, the counts and the image rule see a user's images with no
change of their own. Python's `Body.User(text, images=())`.

### 3. The store: before the pad, and version 3 only once needed (fixed)

An input's images are the session's before any node holds them: `Store::save` writes the images
of the pad's `Input` items (content-addressed, `images/<sha256>`, as D30 §3) before it writes the
pad, and refuses one that carries no bytes and is not kept (`MissingImage`), so a pad never names
an image its directory lacks. `Session.input` and `send` take `Image`s with bytes, or references
the session keeps; `Tree.image_named(sha256)` is the image of that name a node or an input holds,
with its bytes.

`VERSION` goes to 3 and `BASE` is 2. A file is written as 3 only when it holds a record only 3
reads (**fixed**): the log, just before the first `Write` of a `User` node with images (as D30's
v1 log was marked just before its first append); the scratchpad, whenever it holds an input with
images, and as 2 again once it does not. A new session starts at `BASE`; one created holding such
a record (a fork's copy of a walk) starts at 3. So a 0.17 build keeps opening every session that
never held a user image, and refuses one that did by name (`session file version 3; this build
reads 1 to 2`), never on a record it cannot decode.

### 4. The wire: `PROTOCOL` 3

- **`input` and `send`** gain `images: [ImageInput]`, optional, at most 20 (`maxItems` in the
  schema). An **`ImageInput`** is `{"data": "<base64>"}` or `{"sha256": "<hex>"}`: exactly one.
  `data` is the file's bytes in standard base64, decoded strictly (padding required, no
  whitespace, nothing after the padding). `sha256` names an image already in this session (a
  node's or an input's), never a path.
- **`image(sha256) -> base64`** reads one image's bytes, for a client to draw a node's or an
  item's `Image` (which carries only `mime`, `sha256`, `width`, `height`). `NotFound` (a
  `LookupError`, `MissingImage` locally) if the session holds none by that name.
- The new `User` and `Input` shapes appear inside `node`, `walk`, `nodes` and the events, so a
  strict protocol-2 decoder (tau-code, the ratatui example) meets an unknown shape: that is why
  this is 3 and not additive within 2 (the same reason as D30 §8).

**Where it is checked.** The server owns every limit; the client's checks are a courtesy.

| what | where | answer |
|---|---|---|
| more than `MAX_INPUT_IMAGES` (20) | params decode, before any image is decoded | `InvalidParams` |
| base64 that is not strict, bytes over `MAX_INPUT_BYTES` (20 MiB; refused by length before decoding), bytes the server does not sniff as PNG, JPEG, GIF or WebP, both or neither of `data`/`sha256`, a `sha256` that is not 64 lowercase hex | `ImageInput`'s construction, which every decode runs | `BadImage`, on the wire `InvalidParams` |
| a body over 64 MiB on the HTTP edge | the edge, before the JSON is read | `413` |
| a `sha256` the session does not hold | the driver | `MissingImage`, on the wire `NotFound` |
| the cursor's model does not take images, or one is over its provider's limits | the driver, the image rule (D30 §6) | `ValueError` naming why, on the wire `Refused`; nothing is added |

A client's claims (a `mime`, a size) are not fields: an object with them is decoded for `data`
alone, and the server reads the format and size from the bytes. A Unix socket message has no size
cap, so the per-image and per-input caps are enforced where params are decoded, whatever the
transport. The fixed limits (**fixed**, for 0.18) are `tau.MAX_INPUT_IMAGES` and
`tau.MAX_INPUT_BYTES`; a provider's own limits (D30 §6) may be lower.

**No bytes in text.** `ImageInput`'s `repr` is `ImageInput(<N bytes>)`; a refusal never echoes
the data; trace spans name the call, not its arguments; `--verbose` prints no messages.

### 5. Fail early: one rule, checked twice (and the backstop)

`_images.images_problem` is the rule (D30 §6). For a user's images the driver runs it

1. **at `input`/`send`** (`Calls`), against the model of the cursor's branch: a misfit raises
   (`ValueError`, `Refused`) and queues nothing;
2. **after the `pre_input` hooks**, on the draft they leave: a hook may add images (`@path` does,
   §7), so a misfit halts the step with why, `"1 image not sent: the model local:qwen does not
   take images (...)"`, by the existing `pre_input` halt path: the input is removed and its
   client told in a `Notice`.

`walk_problem` before a sample stays the backstop (a branch moved to another model, a node copied
in). `_effects.image_gate` is the rule at a node, built once by the driver; D30's `call_gate` is
now that gate at a batch's node.

### 6. Providers

`_providers/_walk.py::_base_turns` builds `UserTurn(text, images)` from a `User` node, so every
provider sends a user's images where it already sent a `Results`-without-a-batch's (D30 §7): the
text, then one image item per image. An image alone sends no empty text item (OpenAI-compatible,
Anthropic and Gemini alike). Anthropic's manual-thinking rule (`_thinkless_loop`) counts a user
message with images as the user speaking, so an image alone opens a new loop. The **mock
provider** declares `image_limits` (20 MiB, as OpenAI-compatible) and echoes a user message's
images as one more line, `[2 images: image/png 640x480, image/png 32x32]`.

### 7. `@path` attaches an image (D24 §5)

In the `attachments` extension, a file whose bytes sniff as PNG, JPEG, GIF or WebP becomes an
image on the draft, after any images the input had, and the text gains one line,
`[image @shot.png: image/png 640x480]`, where a file's block would be; `typed` keeps what was
typed. The 10 kB `limit` is for text: an image is held to `MAX_INPUT_BYTES` (a bigger one is a
`<reference ... reason="over the 20 MB image limit"/>`) and, after the hooks, to what the model
takes. Every other rule stays: only a `Frontend`'s input, the guards before it, code spans silent,
a file named twice attached once. A template's arguments attach an image file as a file, as before
(a command's text has nowhere to put an image). `tau -p "what is @shot.png"` and the TUI get
images this way with no new flag.

### 8. What else meets a user's image

| where | rule |
|---|---|
| **Counting** (D26) | the existing `ImageRule` (TC-05), one rule for a tool's image and a user's: Anthropic measured patches+3 for a user image against a ceiling of patches+21; Gemini 64×64 measured 1089, under its 1120. No `where` parameter. |
| **Prune** | the same rule as tool images (**fixed**): only turn N keeps its images. Turn N-1's user message becomes a derived node, its images as one `[image WxH mime]` line each after its text; turns 0 to N-2's go into the transcript the same way. |
| **Compaction** | as D30 §9: the summarizer is sent the head with its images, the tail is copied by reference with its user images, the head's images go with the head. |
| **Fork** | copies the walk's user images into the new directory, as a tool's (D30 §3). |
| **TUI, `tau export`, the tree pane** | one `[image WxH mime]` line per image under the message's text. |

## Alternatives

- **Additive within protocol 2** (an optional field a client may ignore): the new `User` shape
  breaks every strict decoder of 2. Rejected, as in D30 §8.
- **Version 3 on the first append** (as D30 did for 2): every session a 0.18 build touched would
  be refused by 0.17, image or not. Rejected (**fixed**).
- **Interleaved parts** (`User { parts: [Text | Image] }`): no provider needs it yet, and every
  reader of `User.text` would change. Rejected for now; text then images.
- **A client-declared `mime`** (`{"data", "mime"}`): the server would have to check the claim
  against the bytes anyway. Rejected: the bytes say what they are.
- **`tau -p --image FILE`**: a second way to say what `@FILE` says. Rejected (**fixed**).
- **Configurable input limits**: deferred; fixed for 0.18 (**fixed**).

## Later slices (#613 §8)

Clients only, 0.18.x: transcript thumbnails through `image()`, edit-and-resend with images, VS
Code's Attach command and Explorer drop, stdin images in `-p`. Later: garbage-collecting images no
node names, harness-side resizing.

## Glossary additions

`ImageInput` (with `ImageInput.of`), `MAX_INPUT_IMAGES`, `MAX_INPUT_BYTES`, `Client.image`,
`Tree.image_named`, `Body.images`; `Body.User.images` and `ItemData.Input.images` in *body* and
*item*; `VERSION` 3 and `BASE` in *store*; `PROTOCOL` 3; the image check's two new places; the
attachments extension's image rule.
