"""TA-05 layer 1: the a2a-sdk client against `tau-a2a` in front of a real `tau serve`.

Card, send, stream, an input-required round trip, cancel, auth-required, one task at a time per
context, contexts as cursors, the tau-fork extension, and bearer-JWT owner scoping."""

import asyncio
import json
import subprocess
import sys
import time
import uuid

import httpx
import jwt
import pytest
from a2a.client import ClientConfig, create_client
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.errors import TaskNotFoundError
from conftest import GATE, MOCK
from cryptography.hazmat.primitives.asymmetric import rsa
from google.protobuf import json_format, struct_pb2

from tau_a2a import FORK_EXTENSION

pytestmark = pytest.mark.asyncio
S = TaskState


def msg(text: str = "", *, context: str = "", task: str = "", data: dict | None = None) -> Message:
    parts = [Part(text=text)] if text else []
    if data is not None:
        parts.append(Part(data=json_format.ParseDict(data, Part().data)))
    return Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=parts,
        context_id=context,
        task_id=task,
    )


async def client(url: str, *, streaming: bool = False, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx.AsyncClient(headers=headers, timeout=60)
    return await create_client(url, ClientConfig(streaming=streaming, httpx_client=http))


async def send(c, message: Message, **config):
    """Every StreamResponse of one send; the last task it reported."""
    req = SendMessageRequest(message=message, configuration=SendMessageConfiguration(**config))
    seen = [r async for r in c.send_message(req)]
    task = next((r.task for r in reversed(seen) if r.HasField("task")), None)
    return seen, task


def text_of(task) -> str:
    return "".join(p.text for a in task.artifacts for p in a.parts)


async def test_card_is_a2a_1_0_jsonrpc_and_offers_tau_fork(serve) -> None:
    s = serve()
    card = httpx.get(s.url + "/.well-known/agent-card.json").json()
    assert card["supportedInterfaces"] == [
        {"url": s.url + "/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    assert card["version"].startswith(s.session + "@")
    assert card["capabilities"]["streaming"] is True
    assert [e["uri"] for e in card["capabilities"]["extensions"]] == [FORK_EXTENSION]
    assert not card["capabilities"]["extensions"][0].get("required", False)
    assert "securitySchemes" not in card


SKILL = "---\ndescription: Sort incoming issues by severity.\n---\nRead the issue, then...\n"


def card_of(s) -> dict:
    return httpx.get(s.url + "/.well-known/agent-card.json").json()


async def test_the_card_skills_are_the_sessions_skills(serve) -> None:
    s = serve(files={"skills/triage/SKILL.md": SKILL})
    skills = card_of(s)["skills"]
    assert [(k["id"], k["description"]) for k in skills] == [
        ("triage", "Sort incoming issues by severity.")
    ]


async def test_a_session_without_skills_offers_converse(serve) -> None:
    assert [k["id"] for k in card_of(serve())["skills"]] == ["converse"]


OVERLAY = {
    "description": "Triage for the issue tracker.",
    "version": "1.2.0+sha256.abc",
    "provider": {"organization": "ffwf", "url": "https://ffwf.net"},
    "skills": [{"id": "triage", "name": "Triage", "description": "Sort issues.", "tags": ["x"]}],
    "capabilities": {
        "extensions": [
            {
                "uri": "https://example.org/ext/definition/v1",
                "params": {"digest": "sha256:abc"},
            }
        ]
    },
}


async def test_a_card_overlay_is_merged_over_the_stated_card(serve) -> None:
    s = serve(
        files={"skills/triage/SKILL.md": SKILL, "{base}/overlay.json": json.dumps(OVERLAY)},
        a2a_args=("--auth", "none", "--card-overlay", "overlay.json"),
    )
    card = card_of(s)
    assert card["description"] == OVERLAY["description"]
    assert card["version"] == OVERLAY["version"]
    assert card["provider"] == OVERLAY["provider"]
    assert card["skills"] == OVERLAY["skills"]
    extensions = card["capabilities"]["extensions"]
    assert [e["uri"] for e in extensions] == [
        FORK_EXTENSION,
        "https://example.org/ext/definition/v1",
    ]
    assert extensions[1]["params"] == {"digest": "sha256:abc"}
    # what the server states stays its own
    assert card["supportedInterfaces"][0]["url"] == s.url + "/"
    assert card["capabilities"]["streaming"] is True


@pytest.mark.parametrize(
    ("overlay", "says"),
    [
        ({"supportedInterfaces": []}, "supportedInterfaces"),
        ({"capabilities": {"streaming": False}}, "capabilities.streaming"),
        ({"skils": []}, "skils"),
        ([], "JSON object"),
    ],
)
async def test_a_card_overlay_that_cannot_be_believed_is_refused(tmp_path, overlay, says) -> None:
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(overlay), encoding="utf-8")
    done = subprocess.run(  # noqa: PLW1510 - the code is asserted with the output
        [
            sys.executable,
            "-m",
            "tau_a2a",
            "--socket",
            str(tmp_path / "none.sock"),
            "--db",
            str(tmp_path / "db"),
            "--auth",
            "none",
            "--card-overlay",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert says in done.stderr


async def test_send_completes_with_the_reply_and_its_evidence(serve) -> None:
    s = serve()
    c = await client(s.url)
    _, task = await send(c, msg("hello"))
    assert task.status.state == S.TASK_STATE_COMPLETED
    assert text_of(task) == "mock: hello"
    meta = json_format.MessageToDict(task.artifacts[0].metadata)["tau"]
    assert meta["tools"] == [] and meta["cursor"] >= 1


async def test_a_context_is_one_cursor_and_a_new_context_another(serve) -> None:
    s = serve()
    c = await client(s.url)
    _, a = await send(c, msg("one"))
    _, b = await send(c, msg("two", context=a.context_id))
    _, other = await send(c, msg("three"))

    def cursor(t):
        return json_format.MessageToDict(t.artifacts[0].metadata)["tau"]["cursor"]

    assert b.context_id == a.context_id and cursor(b) == cursor(a)
    assert other.context_id != a.context_id and cursor(other) != cursor(a)
    # a client-chosen context id is accepted, and is a cursor of its own
    _, mine = await send(c, msg("four", context="room-thread-1"))
    assert mine.context_id == "room-thread-1" and cursor(mine) not in (cursor(a), cursor(other))


async def test_stream_sends_reply_chunks_then_the_completed_task(serve) -> None:
    s = serve(model={**MOCK, "options": {"delay_ms": 5, "reply": "one two three four"}})
    c = await client(s.url, streaming=True)
    seen, _ = await send(c, msg("go"))
    chunks = [r.artifact_update for r in seen if r.HasField("artifact_update")]
    assert len(chunks) >= 4
    states = [r.status_update.status.state for r in seen if r.HasField("status_update")]
    assert states[-1] == S.TASK_STATE_COMPLETED
    final = chunks[-1]
    assert (
        final.last_chunk and "".join(p.text for p in final.artifact.parts) == "one two three four"
    )


async def test_a_question_is_input_required_and_the_answer_continues_the_task(serve) -> None:
    s = serve(extensions=(GATE,))
    c = await client(s.url)
    _, task = await send(c, msg("go"))
    assert task.status.state == S.TASK_STATE_INPUT_REQUIRED
    parts = task.status.message.parts
    assert parts[0].text == "run the thing?"
    asked = json_format.MessageToDict(parts[1].data)
    assert asked["extension"] == "gate" and asked["schema"]["enum"] == ["yes", "no"]

    # a value the schema refuses keeps it waiting, and says why
    _, again = await send(c, msg("maybe", context=task.context_id, task=task.id))
    assert again.status.state == S.TASK_STATE_INPUT_REQUIRED

    _, done = await send(c, msg("yes", context=task.context_id, task=task.id))
    assert done.status.state == S.TASK_STATE_COMPLETED, done.status
    assert text_of(done) == "mock: go"


async def test_the_answer_may_be_a_data_part(serve) -> None:
    s = serve(extensions=(GATE,))
    c = await client(s.url)
    _, task = await send(c, msg("go"))
    answer = Message(
        message_id="d1",
        role=Role.ROLE_USER,
        context_id=task.context_id,
        task_id=task.id,
        parts=[Part(data=struct_pb2.Value(string_value="yes"))],
    )
    _, done = await send(c, answer)
    assert done.status.state == S.TASK_STATE_COMPLETED
    assert text_of(done) == "mock: go"


async def test_cancel_aborts_the_step_and_frees_the_context(serve) -> None:
    s = serve(extensions=(GATE,))
    c = await client(s.url)
    _, task = await send(c, msg("go"))
    assert task.status.state == S.TASK_STATE_INPUT_REQUIRED
    canceled = await c.cancel_task(CancelTaskRequest(id=task.id))
    assert canceled.status.state == S.TASK_STATE_CANCELED
    # the context takes a new task, which asks again (the gate is still there)
    _, next_task = await send(c, msg("again", context=task.context_id))
    assert next_task.status.state == S.TASK_STATE_INPUT_REQUIRED
    assert next_task.id != task.id


async def test_a_new_task_on_a_context_waiting_for_input_is_rejected(serve) -> None:
    s = serve(extensions=(GATE,))
    c = await client(s.url)
    _, task = await send(c, msg("go"))
    _, second = await send(c, msg("meanwhile", context=task.context_id))
    assert second.status.state == S.TASK_STATE_REJECTED


async def test_tasks_on_one_context_run_one_after_another(serve) -> None:
    s = serve(model={**MOCK, "options": {"delay_ms": 40, "reply": "a b c d e"}})
    c = await client(s.url)
    _, first = await send(c, msg("first"))
    ctx = first.context_id
    (_, a), (_, b) = await asyncio.gather(
        send(c, msg("x", context=ctx)), send(c, msg("y", context=ctx))
    )
    assert a.status.state == b.status.state == S.TASK_STATE_COMPLETED

    def node(t):
        return json_format.MessageToDict(t.artifacts[0].metadata)["tau"]["node"]

    assert node(a) != node(b)  # each rested on its own reply: neither swallowed the other


async def test_a_credential_failure_is_auth_required_not_failed(serve, http_stub) -> None:
    base = http_stub(
        401, {"error": {"message": "invalid api key", "type": "invalid_request_error"}}
    )
    s = serve(model={"model": "x", "provider": "openai", "base_url": base + "/v1", "api_key": "k"})
    c = await client(s.url)
    _, task = await send(c, msg("hello"))
    assert task.status.state == S.TASK_STATE_AUTH_REQUIRED, task.status
    got = await c.get_task(GetTaskRequest(id=task.id))
    assert got.status.state == S.TASK_STATE_AUTH_REQUIRED


async def test_tau_fork_opens_a_context_at_the_node_a_task_ended_on(serve) -> None:
    s = serve()
    c = await client(s.url)
    _, a = await send(c, msg("base"))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "f1",
                "role": "ROLE_USER",
                "parts": [{"text": "branch"}],
                "metadata": {FORK_EXTENSION: {"at": a.id}},
            }
        },
    }
    r = httpx.post(
        s.url + "/",
        json=body,
        timeout=60,
        headers={"A2A-Version": "1.0", "A2A-Extensions": FORK_EXTENSION},
    )
    assert "result" in r.json(), r.text
    task = r.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", r.text
    assert task["contextId"] != a.context_id
    meta_a = json_format.MessageToDict(a.artifacts[0].metadata)["tau"]
    meta_f = task["artifacts"][0]["metadata"]["tau"]
    assert meta_f["cursor"] != meta_a["cursor"]
    # the branch continues from the base task's last node: its reply's parent chain holds it
    assert meta_f["node"] > meta_a["node"]

    # an unknown task to fork from is refused
    body["params"]["message"] |= {"messageId": "f2", "metadata": {FORK_EXTENSION: {"at": "nope"}}}
    r = httpx.post(
        s.url + "/",
        json=body,
        timeout=60,
        headers={"A2A-Version": "1.0", "A2A-Extensions": FORK_EXTENSION},
    )
    assert (
        "error" in r.json() or r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_FAILED"
    )


# -- bearer JWT


def keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk |= {"kid": "k1", "use": "sig", "alg": "RS256"}
    return key, {"keys": [jwk]}


def token(
    key, sub: str, *, aud: str = "tau-a2a", iss: str = "https://idp.example", scope: str = "a2a"
) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "aud": aud, "iss": iss, "exp": now + 300, "iat": now, "scope": scope},
        key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )


@pytest.fixture
def jwt_served(serve, http_stub):
    key, jwks = keys()
    url = http_stub(200, jwks)
    s = serve(
        a2a_args=(
            "--auth",
            "jwt",
            "--issuer",
            "https://idp.example",
            "--audience",
            "tau-a2a",
            "--jwks-url",
            url + "/jwks",
            "--scope",
            "a2a",
        )
    )
    return s, key


async def test_jwt_card_is_public_and_declares_bearer_and_oidc(jwt_served) -> None:
    s, _ = jwt_served
    card = httpx.get(s.url + "/.well-known/agent-card.json").json()
    schemes = card["securitySchemes"]
    assert schemes["bearer"]["httpAuthSecurityScheme"] == {
        "scheme": "Bearer",
        "bearerFormat": "JWT",
    }
    assert schemes["oidc"]["openIdConnectSecurityScheme"]["openIdConnectUrl"] == (
        "https://idp.example/.well-known/openid-configuration"
    )


@pytest.mark.parametrize("bad", ["none", "audience", "issuer", "scope", "garbage"])
async def test_jwt_refuses_a_bad_or_missing_token(jwt_served, bad) -> None:
    s, key = jwt_served
    headers = {"A2A-Version": "1.0"}
    if bad != "none":
        headers["Authorization"] = (
            "Bearer "
            + {
                "audience": token(key, "alice", aud="other"),
                "issuer": token(key, "alice", iss="https://evil.example"),
                "scope": token(key, "alice", scope="read"),
                "garbage": "not.a.jwt",
            }[bad]
        )
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
    }
    r = httpx.post(s.url + "/", json=body, headers=headers, timeout=30)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


async def test_jwt_tasks_and_contexts_belong_to_their_sub(jwt_served) -> None:
    s, key = jwt_served
    alice = await client(s.url, token=token(key, "alice"))
    bob = await client(s.url, token=token(key, "bob"))
    _, task = await send(alice, msg("secret", context="shared-name"))
    assert task.status.state == S.TASK_STATE_COMPLETED
    with pytest.raises(TaskNotFoundError):
        await bob.get_task(GetTaskRequest(id=task.id))
    # the same context id from another owner is another cursor
    _, theirs = await send(bob, msg("mine", context="shared-name"))

    def cursor(t):
        return json_format.MessageToDict(t.artifacts[0].metadata)["tau"]["cursor"]

    assert cursor(theirs) != cursor(task)
