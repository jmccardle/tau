# Gemini capability probe — 2026-08-22

Runs of `scripts/gemini_capability_probe.py` against a **new free-tier key**, to
settle O2 (and inform O1) in `docs/ANTHROPIC-GOOGLE-CLIENTS.md`.

All of it is scoped to Google's `generativelanguage.googleapis.com/v1beta`
endpoint. It says nothing about the same model served elsewhere — a locally-run
Gemma reaches τ through the OpenAI-completions provider and shares none of this
wire.

## Files

| File | Model(s) | Status |
|---|---|---|
| `gemma-2026-08-22.json` | `gemma-4-26b-a4b-it` | all four checks valid |
| `gemini-3.6-flash-2026-08-22.json` | `gemini-3.6-flash` | three valid; multimodal correctly not exercised |
| `gemini-2026-08-22.json` | `gemini-2.5-flash-lite`, `gemini-3-flash-preview` | three valid; **multimodal verdict VOID** |

## The void verdict, stated plainly

`gemini-2026-08-22.json` records `multimodal_function_response.verdict =
"rejected"` for `gemini-3-flash-preview`. **Do not use it.**

That run sent a 1×1 PNG. Google refuses a 1×1 PNG as an *ordinary user part* —
"Unable to process input image" — so the 400 on the nested-image arm cannot be
attributed to nesting. The image was the fault, not the field under test.

The probe now proves the image is decodable before drawing any conclusion from a
rejection, and sends a 64×64 PNG. The `gemini-3.6-flash` run is the first with
that control: it reports the check as *not exercised*, naming the refused image,
which is the correct outcome rather than a verdict.

The file is kept because a void measurement that is explained is worth more than
one that was quietly deleted. Nothing downstream should read its multimodal row.

## What stands

| Question | Measured | Models |
|---|---|---|
| Does a `functionCall` arrive carrying an `id`? | **Yes** | gemini-3-flash-preview, gemini-3.6-flash, gemma-4-26b-a4b-it |
| Is an `id` on a `functionResponse` accepted? | **Accepted** (200, control 200) | all three |
| Do two same-name calls in one turn, answered by name only, pair correctly? | **Yes** | all three |
| Is an image nested in `functionResponse.parts` accepted? | **Accepted** | gemma-4-26b-a4b-it only |

`gemma-4-26b-a4b-it` is the load-bearing row. pi's `requiresToolCallId`
(`google-shared.ts:105`) answers **false** for it — no `gemini-N` match, not
`claude-`, not `gpt-oss-` — so it is the reachable instance of "a model that does
not expect an id". Google accepted the id anyway.

That is the sentence O2 said needed measuring:

> For `requires_tool_call_id` it is not obvious — sending an id to a model that
> does not expect one may itself be rejected. That needs measuring before it is
> decided.

Measured: it is not rejected. The permissive branch is safe.

## What could not be measured, and why it may not matter

**No Gemini below major version 3 is callable on a new key.** Both
`gemini-2.5-flash` and `gemini-2.5-flash-lite` return 404, "no longer available
to new users". So the legacy positional case pi's rule protects is unreachable
here.

This is not the same as "unreachable for everyone" — an older key may still hold
2.5 access — so it is a gap in the sample, not proof the case is extinct. It does
mean the risk of defaulting to the permissive branch is smaller than O2 assumed,
because a new integration cannot reach the models the conservative branch exists
for.

`gemma-4-26b-a4b-it` partially covers the gap: it is a model pi treats as
id-less, and it accepted the id. That is a different argument from "we tested an
old Gemini", and weaker, and it is the best available.

## Free-tier limits, measured

From the console's rate-limit table, and confirmed by burning them:

- **5 requests per minute** for most text-out Gemini models (10 for 2.5 Flash Lite).
- **20 requests per day, per model.**

An early build paced at 12 requests/minute and spent Gemini 3.5 Flash's entire
day (22/20) without completing a single measurement. The probe now paces to 5 RPM
by default (`--rpm`) and uses 7–8 requests per model.

Gemma's quota is much larger, which is why it could be measured at `--rpm 15`.
