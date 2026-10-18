# Reasoning on the wire: probe, 2026-10-17

Measurements behind `docs/REASONING-WIRE.md` §1. These are raw HTTP calls (the
Google streaming-placement rows use the `google-genai` SDK). Neither the τ
providers nor the Anthropic SDK are involved. Each arm ran **once**, with the
prompt "Is 391 prime? Think it through, then answer in one line." and
`max_tokens` 2000. Rows: `reasoning-wire-2026-10-17.jsonl`.

The runs were made with a scratch script whose arms
`scripts/reasoning_wire_probe.py` reproduces one for one; the committed script
takes the config entry name as an argument, so it carries no one's config.

## Anthropic: `claude-haiku-5-5`

| Sent | Status | thinking chars | thinking_tokens | output_tokens |
|---|---|---|---|---|
| no `thinking` field | 200 | 0 (signature 956 chars) | 75 | 97 |
| `disabled` | 200 | 0 | 0 | 24 |
| adaptive, summarized, effort `low` | 200 | 92 | 84 | 107 |
| … `medium` | 200 | 42 | 39 | 62 |
| … `high` | 200 | 93 | 67 | 89 |
| … `xhigh` | 200 | 102 | 109 | 132 |
| … `max` | 200 | 316 | 384 | 406 |
| effort `minimal` | **400** | "output_config.effort: Input should be 'low', 'medium', 'high', 'xhigh' or 'max'" | | |
| `disabled` + effort `xhigh` | **400** | "output_config.effort 'xhigh' is not supported when thinking is disabled on this model" | | |

What it settles:

* With no field, the model thinks, and returns a signature with no text.
* `minimal` is not an Anthropic effort.
* `low` through `high` do not order on a one-line prompt: one run each, and
  adaptive thinking decides per request. `max` clearly separates from the rest.
* Whether the summary costs anything **cannot be settled from these numbers**.
  The spread between runs is larger than any difference between arms. The
  "No charge" answer comes from the docs (REASONING-WIRE.md §8).

## Google: `gemini-3.5-flash`

| thinkingConfig | Status | thought chars | thoughtsTokenCount |
|---|---|---|---|
| none | 200 | 0 | 505 |
| `includeThoughts` only | 200 | 1140 | 477 |
| `thinkingLevel: minimal` | 200 | 0 | absent (0) |
| `thinkingLevel: low` | 200 | 950 | 248 |
| `thinkingLevel: medium` | 200 | 1011 | 552 |
| `thinkingLevel: high` | 200 | 1106 | 519 |
| `thinkingLevel: xhigh` | **400** | "Invalid value at '…thinking_level', \"xhigh\"" | |
| `thinkingBudget: 0` | 200 | 0 | absent (0) |
| `thinkingBudget: 1024` | 200 | 764 | 246 |
| level + budget | **400** | "You can only set only one of thinking budget and thinking level." | |

Each 200 carries one part with a `thoughtSignature`, even with no function
call. **Streamed**, that signature arrives on a trailing part with empty text
after the answer: 195 bytes at level low, 107 at minimal.

### Does Gemini read the answer's signature back? (`google-replay`)

Turn 1 at `thinkingLevel: high`: "Pick a secret integer … reply only 'Chosen.'"
It produced 227 thought tokens and a 1176-character signature on the text part.
Turn 2 asked for the number:

| Turn-2 model turn | promptTokenCount | thoughtsTokenCount | answer |
|---|---|---|---|
| signature on the text part | 268 | 125 | 737 |
| signature on a trailing empty part | 268 | 109 | 737 |
| no signature | 41 | 635 | 496 |

The 227-token difference in prompt size is turn 1's thinking, so the API
decrypts the signature and reads it. Both placements were accepted, and both
recalled the same number. Without the signature the model rebuilt its reasoning,
at 635 thought tokens, and answered differently.

## OpenAI: `gpt-5.6-luna`, Chat Completions

| reasoning_effort | Status | reasoning_tokens | completion_tokens |
|---|---|---|---|
| none sent | 200 | 52 | 79 |
| `none` | 200 | 0 | 16 |
| `minimal` | **400** | "Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'." | |
| `low` | 200 | 47 | 73 |
| `medium` | 200 | 73 | 97 |
| `high` | 200 | 76 | 102 |
| `xhigh` | 200 | 88 | 112 |

The message keys were `annotations`, `content`, `refusal` and `role`. No
reasoning text, summary or encrypted item comes back on this wire, so there is
nothing to replay.

## A router: unorouter

`nemotron-3-super-120b-a12b:free` returned plaintext `reasoning_content` (442
chars) and `completion_tokens_details.reasoning_tokens` 219. It sent no
`reasoning_details`. Two other free models answered 503 (rate limited), and
those rows are kept as they came back.

## What was not measured

* No local llama.cpp server was reachable, so `reasoning_effort: "none"` on
  llama.cpp, and whether it ignores the field, is unknown.
* Gemini 2.5 and 3 Pro were not called. Whether 3 Pro rejects
  `thinkingBudget: 0` is unknown, and a 400 there would say so.
* Repeat runs: there are none. Every token count above comes from a single
  run.
