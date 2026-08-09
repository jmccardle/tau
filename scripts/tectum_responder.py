#!/usr/bin/env python
"""Run τ as a tectum agent node, on the bus directly.

This is the demo runner for the "τ on the tectum bus" path: instead of tectum's
``agent_pool`` spawning a ``pi --mode rpc`` subprocess per dispatch, τ subscribes
the schema's inbound subject itself, takes the turn in-process, and publishes
its ``…out.<verb>`` effector events with tectum's own envelope.

Nothing here is a tectum node — τ is a plain external NATS client, the same
posture ``parley-nats`` takes. No schema is written, no supervisor is involved.

Bring-up (each piece verified independently before running this):

    docker run -d --rm --name tau-nats -p 4222:4222 -p 8222:8222 nats:latest -m 8222
    cd ~/Development/jmfts && docker compose up -d     # api on :8100
    curl -s http://192.168.1.100:8080/v1/models        # the LLM
    python scripts/tectum_responder.py                 # this
    cd ~/Development/tectum/parley-nats && parley-nats harness_text --no-up

``parley-nats --no-up`` attaches to the running NATS rather than spawning a
tectum stack, injects typed utterances on
``events.sensation.audio.resolved.clean``, renders ``…out.speak`` in its
scrollback, and publishes the ``events.action.speech.completed.<bid>`` ack this
session's ``speak`` tool blocks on. That closes the loop with no mic and no TTS.

Ctrl-C fires ``session_shutdown``, which unsubscribes and closes the connection.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from typing import Any

from tau_ai.types import Model

from tau_agent_core.sdk import create_agent_session

#: The subject ``praxis/harness_text.yaml`` drives the responder with —
#: parley-nats publishes synthetic utterances here in place of the mic front end.
DEFAULT_INBOUND = "events.sensation.audio.resolved.clean"

#: Kept deliberately close to tectum's persona framing: the agent is one voice in
#: a room, silence is a real option, and the turn ends when it speaks.
SYSTEM_PROMPT = """\
You are Kevin, a presence in a room, listening.

You hear what people say near you. Each thing you hear is one turn.

The ONLY way anyone can hear you is the `speak` tool. Text you write in your
reply is not delivered to anyone — it goes nowhere and no one sees it. If you
want to be heard, you must call `speak`. Writing your answer as text instead of
calling `speak` means you said nothing at all.

Staying silent is a real choice — use it when nothing you could say would add
anything. But silence means calling no tool AND writing nothing, deliberately;
it does not mean typing a reply and hoping it reaches someone.

When you do speak, say one natural, conversational thing. You are talking, not
writing: no lists, no headings, no preamble. Call `speak` at most once per turn,
and once you have spoken your turn is over.
"""


async def main() -> None:
    # Line-buffer stdout: this runs as a long-lived background process whose
    # whole value is the running commentary, and Python block-buffers a
    # redirected stream — which makes a working demo look hung.
    sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", default="responder",
                    help="agent identity; publishes events.workspace.<ws>.out.<verb>")
    ap.add_argument("--inbound-subject", default=DEFAULT_INBOUND)
    ap.add_argument("--nats-url", default="nats://127.0.0.1:4222")
    ap.add_argument("--llm-url", default="http://192.168.1.100:8080/v1")
    ap.add_argument("--model", default="/fast/model/moe-compare/qwen36-35B-IQ4_XS.gguf")
    ap.add_argument("--verbs", default="speak",
                    help="comma-separated effector verbs to register as tools")
    ap.add_argument("--max-tokens", type=int, default=4096,
                    help="hard cap per completion (Model.max_tokens); "
                         "0 leaves generation unbounded (the server default)")
    ap.add_argument("--think", action="store_true",
                    help="leave the chat template's reasoning ON (default: off)")
    args = ap.parse_args()

    ext_path = (
        Path(__file__).resolve().parent.parent
        / "tau-agent-core/src/tau_agent_core/extensions_builtin/nats_bus.py"
    )
    if not ext_path.is_file():
        raise SystemExit(f"extension not found: {ext_path}")

    # An explicit Model, not a bare id string, for two reasons that both bit:
    #
    # `thinking_level="off"` becomes `reasoning_effort`, which the provider only
    # sends when `Model.reasoning` is True — and it defaults False. So for a
    # resolved-from-string model NOTHING was sent, and Qwen3's chat template
    # (which has `enable_thinking`) reasoned by default. The provider only sets
    # `chat_template_kwargs.enable_thinking = False` on grammar-constrained
    # calls, so a plain tool-calling turn had no path to turn it off. Observed:
    # 120k decoded tokens and no tool call. `extra_body` IS that path — a
    # first-class per-model field, not a smuggled payload key.
    #
    # `max_tokens` reaches the wire as of the tau-ai fix in this branch's history
    # ("send Model.max_tokens — it was declared and never consulted"). Before it,
    # a Model declaring max_tokens=512 produced a llama.cpp slot reporting
    # `n_predict = -1`: generation ran unbounded against n_ctx=262144, which is how
    # one turn decoded ~120k tokens while looking like it had simply gone quiet.
    #
    # 4096 is chosen against this model, not plucked. Qwen3.6's reasoning budget is
    # ~2k tokens and observed thinking blocks ran ~3800-4200 chars (~1000-1200
    # tokens), so 4096 leaves reasoning room to close AND the tool call room to
    # land. A tighter cap would truncate mid-reasoning and damage parseability —
    # the opposite of the problem being solved. `--max-tokens 0` opts back out.
    extra_body: dict[str, object] = {}
    if not args.think:
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}
    if not args.max_tokens:
        # An explicit "no cap": the provider skips its own send when the caller has
        # already named one, and llama.cpp reads -1 as unbounded.
        extra_body["max_tokens"] = -1

    model = Model(
        id=args.model,
        name="local",
        api="openai-completions",
        provider="openai",
        base_url=args.llm_url,
        context_window=262144,
        max_tokens=args.max_tokens or 4096,
        extra_body=extra_body,
    )

    session = create_agent_session(
        model=model,
        base_url=args.llm_url,
        # llama.cpp's server ignores the key but the provider Fail-Early raises
        # without one; this states plainly that it is not a secret.
        api_key=os.environ.get("OPENAI_API_KEY", "local-llama-cpp-no-key"),
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        # H8: the extension declares TOUCHES_BUS and the loader refuses it in a
        # session that was not built to allow a bus. This is that allowance.
        bus_available=True,
    )

    result = await session.load_extensions(
        [str(ext_path)],
        discover=False,
        extensions_config={
            "nats_bus": {
                "workspace": args.workspace,
                "inbound_subject": args.inbound_subject,
                "nats_url": args.nats_url,
                "verbs": tuple(v.strip() for v in args.verbs.split(",") if v.strip()),
                "origin_node": "tau",
            }
        },
    )
    if getattr(result, "errors", None):
        raise SystemExit(f"extension load failed: {result.errors}")

    # Observability: the extension's own channels, so a dropped turn or a bad
    # payload is visible in this terminal rather than only on the bus. The
    # session's EventBus has no public accessor yet; a demo runner reaching for
    # ``_events`` is not worth widening the API over.
    bus = session._events
    bus.on("ext:nats_bus:inbound", lambda p: print(
        f"[inbound] {p['subject']} :: "
        f"{p['event'].get('payload', {}).get('text', '')!r}"))
    bus.on("ext:nats_bus:inbound_error", lambda p: print(f"[bad payload] {p}"))
    bus.on("ext:nats_bus:inbound_dropped", lambda p: print(f"[dropped] {p}"))
    bus.on("ext:nats_bus:turn_error", lambda p: print(f"[turn error] {p}"))

    # Turn-level logging. Without this, a turn that reasons for 120k tokens and
    # never calls a tool is indistinguishable from a turn that chose silence —
    # both look like "nothing published, no error", and the second reading is
    # the flattering one. Log what the model actually did, so silence has to be
    # *observed* rather than inferred.
    def _on_agent_event(event: Any) -> None:
        et = event.type
        if et == "turn_start":
            print(f"[turn {event.turn_index}] start")
        elif et == "message_end":
            msg = event.message or {}
            blocks = msg.get("content") or []
            kinds: list[str] = []
            for b in blocks:
                kind = b.get("type") if isinstance(b, dict) else None
                if kind == "thinking":
                    kinds.append(f"thinking({len(b.get('thinking') or '')} chars)")
                elif kind == "text":
                    kinds.append(f"text({len(b.get('text') or '')} chars)")
                elif kind == "toolCall":
                    kinds.append(f"toolCall({b.get('name')})")
                else:
                    kinds.append(str(kind))
            usage = msg.get("usage") or {}
            print(
                f"[message ] stop={msg.get('stop_reason')} "
                f"blocks=[{', '.join(kinds) or 'none'}] "
                f"in={usage.get('input')} out={usage.get('output')}"
            )
            # A turn that produced neither a tool call nor any text, on a model
            # whose reasoning ran long, is the runaway — name it as such.
            if msg.get("stop_reason") == "length":
                print(
                    "[!] stop_reason=length — the completion hit --max-tokens. "
                    "This is a truncated turn, NOT the agent choosing silence."
                )
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    print(f"[text    ] {b['text'][:400]!r}")
        elif et == "tool_execution_start":
            print(f"[tool  ->] {event.tool_name} {event.args}")
        elif et == "tool_execution_end":
            status = "ERROR" if event.is_error else "ok"
            detail = event.result
            print(f"[tool <-  ] {event.tool_name} {status}: {str(detail)[:300]}")
        elif et == "agent_end":
            n = len(event.messages or [])
            print(f"[turn end] {n} message(s) this turn")

    session.subscribe(_on_agent_event)

    await session.emit_session_start("startup")
    tool_names = sorted(session._registry.get_active_tools())
    print(
        f"τ is on the bus as {args.workspace!r}\n"
        f"  listening : {args.inbound_subject}\n"
        f"  publishing: events.workspace.{args.workspace}.out.<{args.verbs}>\n"
        f"  model     : {args.model} @ {args.llm_url}\n"
        f"  nats      : {args.nats_url}\n"
        f"  reasoning : {'ON (--think)' if args.think else 'off via chat_template_kwargs'}\n"
        f"  max_tokens: {args.max_tokens or 'unbounded'}\n"
        f"  tools     : {tool_names or 'NONE'}\n"
        "Ctrl-C to stop."
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        print("\nshutting down…")
        await session.emit_session_shutdown("quit")


if __name__ == "__main__":
    asyncio.run(main())
