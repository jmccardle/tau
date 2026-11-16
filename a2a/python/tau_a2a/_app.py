"""The A2A application: the agent card, the JSON-RPC endpoint, the task store, the auth."""

import json
from collections.abc import Mapping, Sequence
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

from tau import Form
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
    skills: Sequence[AgentSkill] | None = None  # None: the session's own (D24 §6)
    overlay: Mapping[str, object] | None = None  # a partial AgentCard (A2A JSON), merged over


# The card fields only the server can state truly: where it is, how it authenticates, what the
# wire does. An overlay that sets one is refused rather than believed.
SERVER_OWNED = ("supportedInterfaces", "securitySchemes", "securityRequirements")
SERVER_OWNED_CAPABILITIES = ("streaming", "pushNotifications", "extendedAgentCard")


def read_overlay(path: Path) -> dict[str, object]:
    """The card overlay in `path`: a JSON object, part of an A2A AgentCard. `ValueError` naming
    the problem for anything else, or for a field the server owns."""
    overlay = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(overlay, dict):
        raise ValueError(f"{path}: a card overlay is a JSON object (part of an AgentCard)")
    owned = [k for k in SERVER_OWNED if k in overlay]
    capabilities = overlay.get("capabilities", {})
    if not isinstance(capabilities, dict):
        raise ValueError(f"{path}: capabilities is an object")
    owned += [f"capabilities.{k}" for k in SERVER_OWNED_CAPABILITIES if k in capabilities]
    if owned:
        raise ValueError(f"{path}: the server states {', '.join(owned)}; an overlay cannot")
    overlaid(AgentCard(), overlay)  # every field an AgentCard field, of its type
    return overlay


def overlaid(card: AgentCard, overlay: Mapping[str, object]) -> AgentCard:
    """`card` with `overlay` merged over it: each field the overlay gives replaces the card's,
    except `capabilities`, whose `extensions` are added to the card's (an overlay extension with
    the URI of one the card has replaces it). `ValueError` for what is not an AgentCard field."""
    merged = json_format.MessageToDict(card)
    for key, value in overlay.items():
        if key != "capabilities":
            merged[key] = value
            continue
        own = merged.setdefault("capabilities", {})
        added = list(value.get("extensions", []))  # type: ignore[union-attr]
        uris = {e.get("uri") for e in added}
        own["extensions"] = [e for e in own.get("extensions", []) if e["uri"] not in uris] + added
        own.update({k: v for k, v in value.items() if k != "extensions"})  # type: ignore[union-attr]
    try:
        return json_format.ParseDict(merged, AgentCard())
    except json_format.ParseError as e:
        raise ValueError(f"card overlay: {e}") from e


def skills_of(forms: Sequence[Form]) -> list[AgentSkill]:
    """The card skills of a session: the commands of its `skills` extension, which are the
    session's skills and prompt templates (D24 §6), each with its description."""
    return [
        AgentSkill(id=f.name, name=f.name, description=f.description or f.name, tags=["tau"])
        for f in forms
        if f.extension == "skills"
    ]


def agent_card(s: Settings, version: str, skills: Sequence[AgentSkill] = ()) -> AgentCard:
    """The card the server states, before any overlay: `skills` when there are any, else the one
    skill `converse`."""
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
        skills=list(skills)
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
    return overlaid(card, s.overlay) if s.overlay else card


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
        skills = s.skills if s.skills is not None else skills_of(await link.client.commands())
        card = agent_card(s, version, skills)
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
