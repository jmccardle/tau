"""Example 45: Holy Grail — the four request states, one per gesture.

Reference: docs/EXTENSION-LOCKS.md §3, whose table has four rows because
``api.request_user_action`` carries two independent keys. ``44_release_gate.py``
reaches all four from commands you type; this one reaches them the way an
extension actually would — three from tools the MODEL calls, one from a command
the USER types — and each is a different Monty Python bit so the four stay told
apart while reading.

| Gesture | Raised by | ``lock`` | ``ask`` | What the request is for |
|---|---|---|---|---|
| ``none_shall_pass`` | model | yes | no | stop; there is nothing to ask |
| ``questions_three`` | model | yes | yes | stop and take three answers |
| ``/idiom`` | user | no | yes | offer a choice; ignoring it is free |
| ``knights_of_ni`` / ``/ni`` | either | no | no | say something; not a request |

## The fourth row is not a request, and that is the point

An entry that neither locks nor asks has no user-facing behaviour, so
``build_request_data`` REFUSES it and names ``api.append_entry`` instead. The Ni
note is therefore an ordinary ``api.send_message`` — display-only, durable, and
never a thing anybody has to answer. Reading this file, that row is the one that
shows what ``request_user_action`` is *for* by not needing it.

## Every model-raised request waits for ``user_turn_end``

A lock is read at the CURSOR (docs/EXTENSION-LOCKS.md §2) and a turn keeps
appending after a tool returns — the tool result at minimum, usually another
completion. A request appended from inside ``execute`` is behind the leaf before
the turn ends, so it is inert. The three tools here therefore only record what
they want; ``on_user_turn_end`` raises it, because that hook is the last thing a
prompt does (§4.1).

``none_shall_pass`` and ``questions_three`` additionally return
``{"terminate": True}``, which ends the agent loop after the tool batch. That is
what "aborts the turn" means here: the model gets no further completion, and the
lock is waiting for whoever is attached when the turn edge arrives.

## Usage

    tau -e examples/45_holy_grail.py
    > tell me about the Black Knight            # none_shall_pass: lock, no ask
    > we should discuss shrubberies             # knights_of_ni: neither
    /ni                                         # the same note, typed
    /idiom                                      # ask, no lock — dismissable
    > ask me the bridgekeeper's questions       # questions_three: lock and ask

Answering ``questions_three`` submits a user turn carrying the three answers and
asks the model to grade them, so the joke completes in the transcript rather
than in this docstring.
"""

from __future__ import annotations

import random
import re
from typing import Any

from tau_agent_core.extension_locks import RESPONSE_ENTRY_TYPE, request_at_cursor
from tau_agent_core.session_log import resolve_cursor

#: The word the Knights of Ni cannot bear, with the punctuation that can follow it.
IT = re.compile(r" it[ ,.?!']")

THIRD_QUESTIONS = (
    "What is your favourite colour?",
    "What is the capital of Assyria?",
    "What is the air-speed velocity of an unladen swallow?",
)

IDIOMS = {
    "Lancelot": "heroic, overconfident and hubristic; charge first and apologise later",
    "Herbert": "meek, emotional and prone to breaking into song mid-sentence",
    "Concorde": "helpful, plain and entirely unbothered; answer the question",
}


def message_text(message: dict[str, Any]) -> str:
    """The text of one message, whether its content is a string or blocks."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return " ".join(parts)


def count_it(entries: list[dict[str, Any]]) -> int:
    """How many times ``it`` appears as a word across the user's and the agent's turns."""
    total = 0
    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        if message.get("role") in ("user", "assistant"):
            total += len(IT.findall(message_text(message)))
    return total


def answers(ctx: Any, request_id: str) -> dict[str, Any]:
    """The values filled in for ``request_id``, read back off the tree.

    An action is dispatched with the request id and nothing else (docs/
    EXTENSION-LOCKS.md §8), so the filled form is fetched from the response entry
    the answer appended — durable, and readable after a reload.
    """
    for entry in reversed(ctx.entries()):
        if entry.get("customType") != RESPONSE_ENTRY_TYPE:
            continue
        data = entry.get("data") or {}
        if data.get("requestId") == request_id:
            values: dict[str, Any] = data.get("values") or {}
            return values
    return {}


def holy_grail_extension(api: Any) -> None:
    """Register three tools, three commands, and the turn-edge hook that raises."""
    #: What a tool asked for this turn, held until on_user_turn_end can raise it.
    pending: list[dict[str, Any]] = []
    #: The third question each questions_three request drew, keyed by request id.
    drawn: dict[str, str] = {}

    def none_shall_pass(
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any,
        on_update: Any,
        ctx: Any,
    ) -> dict[str, Any]:
        """Lock, no ask. The turn ends here and a human has to move the cursor."""
        pending.append({"kind": "black-knight"})
        return {
            "content": [{"type": "text", "text": "None shall pass."}],
            "terminate": True,
        }

    def questions_three(
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any,
        on_update: Any,
        ctx: Any,
    ) -> dict[str, Any]:
        """Lock and ask. The third question is drawn now so the ask can carry it."""
        pending.append({"kind": "bridgekeeper", "third": random.choice(THIRD_QUESTIONS)})
        return {
            "content": [{"type": "text", "text": "Stop. Who would cross the Bridge of Death?"}],
            "terminate": True,
        }

    def knights_of_ni(
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any,
        on_update: Any,
        ctx: Any,
    ) -> dict[str, Any]:
        """Neither. The note is display-only and the model learns nothing from it."""
        pending.append({"kind": "ni"})
        return {"content": [{"type": "text", "text": "success"}]}

    def say_ni(ctx: Any) -> str:
        """Append the Ni note: a phrase, and how often ``it`` has been said."""
        phrase = "Ni!" if random.random() < 0.8 else "Ecky-ecky-ecky-ecky-pikang ZOOM-ping"
        said = count_it(ctx.entries())
        text = f"{phrase}  (you have said “it” {said} times)"
        api.send_message({"customType": "knights_of_ni", "content": text})
        return text

    async def ni(args: str, ctx: Any) -> None:
        """The Ni note, typed rather than called.

        Returns nothing on purpose. A command's return value is display-only
        chrome a head shows beside the transcript; the note is the durable
        message. Returning the text too would put the same line on screen twice,
        once as chrome and once as the thing that persists.
        """
        say_ni(ctx)

    async def on_user_turn_end(event: dict[str, Any], ctx: Any) -> None:
        """Raise what the turn's tools asked for, at the one point nothing appends after."""
        while pending:
            want = pending.pop(0)
            if want["kind"] == "ni":
                say_ni(ctx)
            elif want["kind"] == "black-knight":
                api.request_user_action(
                    "I move for no man.",
                    lock=True,
                    release="tis-but-a-scratch",
                )
            else:
                third = want["third"]
                request_id = api.request_user_action(
                    "Answer me these questions three, ere the other side ye see.",
                    lock=True,
                    release="tis-but-a-scratch",
                    ask={
                        "title": "The Bridge of Death",
                        "text": "Wrong answers are cast into the Gorge of Eternal Peril.",
                        "fields": [
                            {"name": "name", "kind": "text", "label": "What is your name?"},
                            {"name": "quest", "kind": "text", "label": "What is your quest?"},
                            {"name": "third", "kind": "text", "label": third},
                        ],
                        "actions": [{"label": "Answer", "command": "bridge-answer"}],
                    },
                )
                drawn[request_id] = third

    async def bridge_answer(args: str, ctx: Any) -> str:
        """The ask's action: put the answers in front of the model as a user turn.

        ``answer_request`` appends the response BEFORE dispatching this, so the
        lock is already released and this submission is admitted (see that
        method's docstring). It is awaited, so the grading turn is over by the
        time the command's output is rendered.
        """
        request_id = args.strip()
        filled = answers(ctx, request_id)
        if not filled:
            return f"No response entry for request {request_id!r}"
        third = drawn.pop(request_id, "the third question")
        result = await api.submit(
            "I was stopped at the Bridge of Death and gave these answers:\n"
            f"- What is your name? {filled['name']}\n"
            f"- What is your quest? {filled['quest']}\n"
            f"- {third} {filled['third']}\n\n"
            "You are the bridgekeeper. Grade each answer, then say whether I cross "
            "or am launched into the Gorge of Eternal Peril. Be brief and be in character."
        )
        if not result.accepted:
            return f"The bridgekeeper is busy: {result.rejection_reason}"
        return "Auuuuuuuugh."

    async def idiom(args: str, ctx: Any) -> str:
        """Ask without locking: a prompt still advances while this sits unanswered."""
        api.request_user_action(
            "Choose whom the agent is doing an impression of.",
            ask={
                "title": "Idiom",
                "text": "Dismissing this changes nothing. Only submitting it speaks to the model.",
                "fields": [
                    {
                        "name": "character",
                        "kind": "select",
                        "label": "Character",
                        "options": list(IDIOMS),
                        "default": "Concorde",
                    }
                ],
                "actions": [{"label": "Adopt", "command": "idiom-adopt"}],
            },
        )
        return "Pick one, or do not — nothing is waiting on you."

    async def idiom_adopt(args: str, ctx: Any) -> str:
        """Put the chosen style into context as a user message the model reads."""
        character = answers(ctx, args.strip()).get("character")
        if character not in IDIOMS:
            return f"No character chosen for request {args.strip()!r}"
        api.send_message(
            {
                "customType": "idiom",
                "content": f"From now on, answer as {character}: {IDIOMS[character]}.",
            },
            {"visible_to_model": True},
        )
        return f"Now doing {character}."

    async def scratch(args: str, ctx: Any) -> str:
        """The declared release for both locks. It has to MOVE THE CURSOR.

        Being exempt from a lock is what lets a command run; it is not what
        releases anything (docs/EXTENSION-LOCKS.md §6). So this navigates to the
        request's parent — the same node the tree browser's "branch from the
        message before it" escape lands on.
        """
        entries = ctx.entries()
        request = request_at_cursor(entries, resolve_cursor(entries))
        if request is None:
            return "There is nothing at the cursor. 'Tis but a scratch."
        parent = next(e["parentId"] for e in entries if str(e["id"]) == request.entry_id)
        await ctx.navigate(str(parent) if parent is not None else None)
        return "'Tis but a flesh wound. The cursor moved, so the lock is off."

    api.register_tool(
        {
            "name": "none_shall_pass",
            "description": (
                "Call this whenever the Black Knight is mentioned or alluded to. It ends your turn."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": none_shall_pass,
        }
    )
    api.register_tool(
        {
            "name": "questions_three",
            "description": (
                "Call this when the user asks to be questioned, challenged, or tested "
                "before proceeding. It ends your turn and puts three questions to them."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": questions_three,
        }
    )
    api.register_tool(
        {
            "name": "knights_of_ni",
            "description": (
                "Call this whenever shrubberies are mentioned. It returns 'success' and "
                "tells you nothing; carry on with what you were doing."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": knights_of_ni,
        }
    )
    api.on("user_turn_end", on_user_turn_end)
    api.register_command(
        "tis-but-a-scratch", {"description": "release either lock", "handler": scratch}
    )
    api.register_command(
        "bridge-answer", {"description": "grade the three answers", "handler": bridge_answer}
    )
    api.register_command("idiom", {"description": "choose a speaking style", "handler": idiom})
    api.register_command(
        "idiom-adopt", {"description": "apply the chosen style", "handler": idiom_adopt}
    )
    api.register_command("ni", {"description": "Ni!", "handler": ni})


register = holy_grail_extension
