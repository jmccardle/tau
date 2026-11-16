"""The A2A application: the agent card, the JSON-RPC endpoint, the task store, the auth."""

from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill
from google.protobuf import json_format
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware

from tau_a2a._auth import JwtBackend, JwtSettings, RequireCaller, unauthorized
from tau_a2a._executor import FORK_EXTENSION, TauExecutor
from tau_a2a._link import Link
from tau_a2a._store import Store

RPC_PATH = "/"


@dataclass(frozen=True)
class Settings:
    socket: str
    db: Path
    url: str  # how callers reach this server, as the card advertises it
    name: str = "tau"
    description: str = "A τ session: each A2A context is a conversation of its own."
    version: str | None = None  # None: "<session id>@<fork node>"
    fork_at: int | None = None  # None: the end of the session's prefix
    client_id: str = "a2a"
    jwt: JwtSettings | None = None
    skills: Sequence[str] = ()


def agent_card(s: Settings, version: str) -> AgentCard:
    card = AgentCard(
        name=s.name,
        description=s.description,
        version=version,
        supported_interfaces=[
            AgentInterface(url=s.url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extensions=[
                AgentExtension(
                    uri=FORK_EXTENSION,
                    required=False,
                    description="Start a new context from the node another of the caller's "
                    'tasks ended on: message metadata {URI: {"at": taskId}}, with the extension '
                    "requested.",
                )
            ],
        ),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id=k, name=k, description=k, tags=["tau"]) for k in s.skills]
        or [
            AgentSkill(
                id="converse",
                name="converse",
                description="Talk to the τ agent; it uses "
                "its tools and may ask a question back (input-required).",
                tags=["tau"],
            )
        ],
    )
    if s.jwt is not None:
        schemes = {
            "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer", "bearerFormat": "JWT"}},
            "oidc": {"openIdConnectSecurityScheme": {"openIdConnectUrl": s.jwt.discovery_url}},
        }
        for name, scheme in schemes.items():
            json_format.ParseDict(scheme, card.security_schemes[name])
        json_format.ParseDict(
            {"schemes": {"bearer": {"list": list(s.jwt.scopes)}}}, card.security_requirements.add()
        )
    return card


def create_app(s: Settings) -> Starlette:
    """The server as an ASGI app. The session's socket is connected on startup."""
    state: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(app: Starlette):
        link = await Link.connect(s.socket, s.client_id)
        fork_at = s.fork_at if s.fork_at is not None else await link.prefix_end()
        version = s.version or f"{link.client.welcome.session}@{fork_at}"
        store = Store(s.db.with_name(s.db.name + ".contexts"))
        engine = create_async_engine(f"sqlite+aiosqlite:///{s.db}")
        tasks = DatabaseTaskStore(engine)
        await tasks.initialize()
        card = agent_card(s, version)
        handler = DefaultRequestHandler(TauExecutor(link, store, fork_at), tasks, card)
        app.router.routes.extend(create_agent_card_routes(card))
        app.router.routes.extend(create_jsonrpc_routes(handler, RPC_PATH))
        state.update(link=link, store=store, card=card)
        try:
            yield
        finally:
            await link.close()
            store.close()
            await engine.dispose()

    middleware = []
    if s.jwt is not None:
        middleware = [
            Middleware(AuthenticationMiddleware, backend=JwtBackend(s.jwt), on_error=unauthorized),
            Middleware(RequireCaller, path=RPC_PATH),
        ]
    app = Starlette(lifespan=lifespan, middleware=middleware)
    app.state.tau = state
    return app
