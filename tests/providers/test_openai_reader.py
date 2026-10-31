"""The OpenAI reader's half of the `ReplyReader` contract: what a failed chunk keeps, refusals, and
the base_url that names the endpoint."""

import json

from harness import scenario
from wire import DONE_LINE, OPENAI, chunk, collect, fresh_walk

from tau_next import Finish, ModelConfig, Part, Stop
from tau_next.providers.openai import OpenAIProvider


@scenario
async def test_calls_a_chunk_finished_before_it_failed_are_kept() -> None:
    first = {"index": 0, "id": "a", "function": {"name": "ta", "arguments": "{}"}}
    second = {"index": 1, "function": {"arguments": "{}"}}  # no id, no name
    choice = {"index": 0, "delta": {"tool_calls": [first, second]}, "finish_reason": "tool_calls"}
    text = "data: " + json.dumps({"choices": [choice]}) + "\n\n"
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert items == [Part.Call("a", "ta", "{}")]
    assert error is not None
    assert "no id or no name" in str(error)
    assert "a call was cut off and dropped" in str(error)


@scenario
async def test_a_refusal_is_text_and_the_reply_ends_as_end() -> None:
    text = chunk({"refusal": "I can't"}) + chunk({"refusal": " help"}, "stop") + DONE_LINE
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert error is None
    assert items == [Part.Text("I can't help"), Finish(Stop.End, "stop")]


def test_check_refuses_a_base_url_that_names_the_endpoint() -> None:
    model = ModelConfig("m", "http://host:8100/v1/chat/completions")
    (problem,) = OpenAIProvider(environ={}).check(model)
    assert "adds /chat/completions itself" in problem
