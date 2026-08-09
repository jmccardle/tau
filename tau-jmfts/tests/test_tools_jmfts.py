"""W15 tools: the agent can retrieve, and cite (JMFTS-INTEGRATION-PLAN.md Phase 5).

Exercises the registered tools through their real ``execute`` signature against a live
server and a real JMFTS-backed session.

Marker: ``jmfts``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI
from tau_ai.types import Model
from tau_jmfts.catalog import JmftsSessionCatalog
from tau_jmfts.client import JmftsClient
from tau_jmfts.ext import tools as tools_ext
from tau_jmfts.ext.enrich import enrich_conversation

pytestmark = pytest.mark.jmfts

TEST_PREFIX = "tau-jmfts-test"


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


@pytest.fixture
def client(jmfts_url: str, jmfts_token: str | None):
    c = JmftsClient(jmfts_url, token=jmfts_token)
    yield c
    c.close()


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:8]


class _Registry:
    """Captures what register() hands to api.register_tool, so the tools can be driven
    exactly as the agent loop would drive them."""

    def __init__(self, session: AgentSession, config: dict[str, Any] | None = None) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self._api = ExtensionAPI(session=session)
        self._config = config or {}

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    def register_tool(self, definition: dict[str, Any]) -> None:
        self.tools[definition["name"]] = definition

    @property
    def ctx(self) -> Any:
        return self._api.context

    async def call(self, name: str, **params: Any) -> dict[str, Any]:
        execute = self.tools[name]["execute"]
        return await execute("call-1", params, None, None, self.ctx)


def _wire(session: AgentSession, **config: Any) -> _Registry:
    registry = _Registry(session, config)
    tools_ext.register(registry)
    return registry


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _bound(catalog: JmftsSessionCatalog, run_id: str) -> tuple[AgentSession, Any]:
    log = catalog.create(
        f"/tmp/{TEST_PREFIX}-{run_id}", "test-model", "test-backend", system_prompt="sys"
    )
    session = AgentSession(
        session_log=log, model=_model(), system_prompt="", tools=[], api_key="k"
    )
    return session, log


def _text_of(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


async def test_the_agent_can_search_its_own_conversation_and_cite_the_hit(
    client: JmftsClient, run_id: str
) -> None:
    """The point of the whole integration: the model asks a question of its own history
    and gets back documents it can NAME. A hit with no id can only be paraphrased, and a
    paraphrase with no citation is indistinguishable from a fabrication."""
    catalog = JmftsSessionCatalog(client)
    session, log = _bound(catalog, run_id)
    try:
        log.append_message(_msg("user", "What did we decide about the Redis pool?"))
        log.append_message(
            _msg("assistant", "We raised maxTotal to 128 and enabled testOnBorrow.")
        )
        enrich_conversation(client, log.root_doc_id)

        api = _wire(session)
        result = await api.call("jmfts_search", query="redis connection pool settings")

        body = _text_of(result)
        assert "maxTotal" in body, "the conversation's own answer was not retrieved"
        assert "[doc " in body, "hits must carry document ids the model can cite"
        assert result["details"]["parent_id"] == log.root_doc_id
    finally:
        catalog.delete(str(log.root_doc_id))


async def test_scope_conversation_does_not_leak_into_other_conversations(
    client: JmftsClient, run_id: str
) -> None:
    catalog = JmftsSessionCatalog(client)
    session, mine = _bound(catalog, run_id)
    _, theirs = _bound(catalog, run_id + "b")
    try:
        secret = f"quokka{run_id}"
        theirs.append_message(_msg("assistant", f"The deployment codename is {secret}."))
        mine.append_message(_msg("assistant", "This conversation is about something else."))
        enrich_conversation(client, mine.root_doc_id)
        enrich_conversation(client, theirs.root_doc_id)

        api = _wire(session)
        body = _text_of(await api.call("jmfts_search", query="deployment codename"))
        assert secret not in body, "scope='conversation' leaked another conversation's content"
    finally:
        catalog.delete(str(mine.root_doc_id))
        catalog.delete(str(theirs.root_doc_id))


async def test_read_returns_the_full_document_behind_a_hit(
    client: JmftsClient, run_id: str
) -> None:
    """Search snippets are truncated; jmfts_read is how the model recovers the rest. The
    truncation SAYS it is truncated -- a clean cut that reads as the whole thing is how a
    model ends up confidently answering from half a document."""
    catalog = JmftsSessionCatalog(client)
    session, log = _bound(catalog, run_id)
    try:
        long_text = f"marker-{run_id}. " + ("Detailed reasoning about the retry path. " * 60)
        log.append_message(_msg("assistant", long_text))
        enrich_conversation(client, log.root_doc_id)

        api = _wire(session)
        hits = await api.call("jmfts_search", query=f"marker-{run_id}")
        assert "truncated" in _text_of(hits), "a cut snippet must announce that it was cut"

        doc_id = hits["details"] and int(_text_of(hits).split("[doc ")[1].split("]")[0])
        body = _text_of(await api.call("jmfts_read", doc_id=doc_id))
        assert f"marker-{run_id}" in body
    finally:
        catalog.delete(str(log.root_doc_id))


async def test_ingest_files_a_document_that_is_immediately_findable(
    client: JmftsClient, run_id: str
) -> None:
    """An ingested document nobody can find is a write to /dev/null, so ingest embeds on
    the way in (it is a tool call — the agent is already waiting — so there is nothing to
    defer, unlike the session write path)."""
    catalog = JmftsSessionCatalog(client)
    session, log = _bound(catalog, run_id)
    doc_id = None
    try:
        api = _wire(session)
        result = await api.call(
            "jmfts_ingest",
            title=f"{TEST_PREFIX}-note-{run_id}",
            content=f"The wombat protocol requires a handshake token called {run_id}.",
        )
        doc_id = result["details"]["doc_id"]

        hits = client.search("wombat protocol handshake", method="vector", limit=5)
        assert doc_id in {h["document"]["id"] for h in hits}, "the ingested doc is not findable"
    finally:
        if doc_id is not None:
            client.delete_document(doc_id)
        catalog.delete(str(log.root_doc_id))


async def test_the_agent_cannot_forge_a_tau_entry_via_ingest(
    client: JmftsClient, run_id: str
) -> None:
    """`tau:*` is τ's own namespace: the loader reads those documents as conversation
    ENTRIES. Letting a model mint one would let it write history into a conversation
    tree — entries nobody appended, indistinguishable from real ones."""
    catalog = JmftsSessionCatalog(client)
    session, log = _bound(catalog, run_id)
    try:
        api = _wire(session)
        with pytest.raises(ValueError, match="must not start with 'tau:'"):
            await api.call(
                "jmfts_ingest", title="forged", content="I never said this", usetype="tau:message"
            )
    finally:
        catalog.delete(str(log.root_doc_id))


async def test_retrieval_is_recorded_in_the_tree_not_injected(
    client: JmftsClient, run_id: str
) -> None:
    """Tree-as-truth. The standard RAG move staples retrieved text into the prompt behind
    the user's back, so what the model read is unrecoverable afterwards. Doing retrieval
    as a TOOL means the call and its results are ordinary toolCall/toolResult entries:
    the search does not mutate the log by itself, and when the loop records the call, it
    is on the path, inspectable, and compacts like anything else.
    """
    catalog = JmftsSessionCatalog(client)
    session, log = _bound(catalog, run_id)
    try:
        log.append_message(_msg("assistant", "We raised maxTotal to 128."))
        enrich_conversation(client, log.root_doc_id)
        before = len(log.entries())
        cursor_before = log.cursor

        api = _wire(session)
        await api.call("jmfts_search", query="redis pool")

        assert len(log.entries()) == before, "the tool wrote to the tree out of band"
        assert log.cursor == cursor_before, "the tool moved the cursor behind the loop's back"
    finally:
        catalog.delete(str(log.root_doc_id))
