# Backend compatibility probe

Runs τ against a set of OpenAI-compatible endpoints and compares what each
server actually accepts with what `tau_llm.compat.detect_compat` claims about
it. Written 2026-08-21 to check the `compat` work in `00601cc` against real
servers rather than against fixtures.

```bash
venv/bin/python experiments/backend-probe/probe_backends.py            # everything
venv/bin/python experiments/backend-probe/probe_backends.py --only ollama
venv/bin/python experiments/backend-probe/probe_backends.py --json out.json
```

Endpoints are a list at the bottom of the file. API keys are read from
`~/.tau/config.json`; nothing is printed that would disclose one.

## What it measures

The three `raw_*` probes go **under** τ, with `httpx` directly. τ's job is to
*pick* a spelling, so asking τ which one the server takes would be circular.
The three `tau_*` probes then run the real provider path end to end.

| Probe | Question |
|---|---|
| `raw_max_tokens` | does a bare `max_tokens` request succeed? |
| `raw_max_completion_tokens` | does `max_completion_tokens` succeed? |
| `raw_stream_options` | does `stream_options: {include_usage: true}` succeed? |
| `tau_stream` | does τ complete a streamed turn, with deltas and usage? |
| `tau_buffered` | does τ complete a `stream: false` turn? |
| `tau_tool_call` | does a streamed tool call arrive with a name and arguments? |

Every raw probe also records **retry evidence** — the response status, whichever
of `retry-after`, `x-should-retry` and the `x-ratelimit-*` family arrived, and
the body on a rejection. It is recorded on success as well as on failure, and an
empty `present` list is a result rather than a gap: a 429 that names no interval
means a backoff has nothing to honour and must fall back to exponential, and that
is the thing worth knowing before writing one.

This measurement has to sit under τ, not through it. τ reads no response headers
anywhere: `_error_event_from_response` (`providers/openai.py:1483`) formats the
status into a message string and discards the rest. Asking τ what a server said
about retrying would return nothing on every backend, which measures τ rather
than the backend.

## Reading the output honestly

Three failure modes look like "this server rejects this field" and are not.
Each cost real time before the harness learned to separate them, so they are
classified as **unevaluated** (`-`) rather than as a rejection:

1. **429.** The tier answered before the body was read. UnoRouter's free tier
   allows one request per minute *per model*, which fails five of six probes;
   `Endpoint.gap` paces them.
2. **5xx.** Measured against UnoRouter, whose non-streaming path takes 95–125s
   and trips Cloudflare's 100s ceiling with a `524`. That read as "rejects
   `max_tokens`" until the identical field returned `200` in 1.6s on the
   streamed path.
3. **A transport exception.** Same reasoning.

A fourth trap is not the harness's to fix: **a model too weak to call a tool is
not a broken backend.** Llama-3.2-1B under raw llama.cpp and Qwen2.5-Coder-0.5B
under vLLM both accept the tool request and answer in prose. Check `stop_reason`
and the raw stream before blaming the transport — and check `max_tokens`, since
a tool call truncated by the cap reports `stop=length` and zero calls.

## Result, 2026-08-21

`results-2026-08-21.json` holds the run. Every reachable backend passed all
three τ paths. `detect_compat` was correct everywhere: no server tested rejects
`max_tokens`, and the two that also accept `max_completion_tokens` accept both.

Not covered: LM Studio (desktop app was not running and `lms` could not wake
it), and OpenAI/Anthropic/Gemini (the keys in `~/.tau/config.json` are the
literal placeholder `your-api-key-here`).
