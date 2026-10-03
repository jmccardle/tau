"""Session reads as the JSON every remote head is sent (docs/TAU-SERVE.md §5).

Each function here is one read a head outside the process needs, computed once.
The RPC verbs and ``tau serve`` both answer from these, so the stdio wire and the
daemon cannot come to disagree about what a session holds.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tau_agent_core.capabilities import Vocabulary
from tau_agent_core.commands import FRONTEND_COMMANDS
from tau_agent_core.flows import Ready, enumerate_domain, next_step

if TYPE_CHECKING:
    from tau_agent_core.conversation_tree import ConversationTree, MessageIdScope
    from tau_agent_core.sdk import LoadExtensionsResult

__all__ = [
    "browse_rows",
    "command_vocabulary",
    "domain_listing",
    "extension_state",
    "flow_next_step",
    "model_catalog",
    "path_completion",
    "resolver_error_message",
]


def command_vocabulary(session: Any) -> list[dict[str, Any]]:
    """Every slash command ``session`` answers, in ``resolve_command``'s precedence order.

    Args:
        session: The :class:`~tau_agent_core.agent_session.AgentSession`.

    Returns:
        ``{name, description, origin, flow, hidden}`` per command: τ's built-ins
        (``origin`` ``"builtin"``), then extension commands by typed name, then
        their qualified names (``hidden``). ``flow`` says whether ``next_step``
        steps it.
    """
    vocabulary = session.vocabulary
    listed: list[dict[str, Any]] = [
        {
            "name": name,
            "description": description,
            "origin": "builtin",
            "flow": name not in vocabulary.views,
            "hidden": False,
        }
        for name, description in FRONTEND_COMMANDS.items()
    ]
    for name, description in session.get_extension_commands():
        if name in FRONTEND_COMMANDS:
            continue
        listed.append(
            {
                "name": name,
                "description": description,
                "origin": "extension",
                "flow": vocabulary.is_extension_flow(name),
                "hidden": False,
            }
        )
    for name, description in session.get_qualified_commands():
        listed.append(
            {
                "name": name,
                "description": description,
                "origin": "extension",
                "flow": vocabulary.is_extension_flow(name),
                "hidden": True,
            }
        )
    return listed


def flow_next_step(
    flow: str, bound: dict[str, Any] | None, cursor: str | None, vocabulary: Vocabulary
) -> dict[str, Any]:
    """:func:`~tau_agent_core.flows.next_step` as ``{status, step, ready}``.

    Raises:
        UnknownFlowError: no flow has that name.
    """
    outcome = next_step(flow, bound, cursor, vocabulary=vocabulary)
    if isinstance(outcome, Ready):
        return {"status": "ready", "ready": asdict(outcome), "step": None}
    return {"status": "step", "step": asdict(outcome), "ready": None}


def domain_listing(
    domain: str,
    *,
    session: Any,
    runtime: Any,
    scope: MessageIdScope | None,
    cursor: str | None,
    query: str,
    limit: int,
) -> dict[str, Any]:
    """:func:`~tau_agent_core.flows.enumerate_domain` as ``{domain, values, total}``.

    ``runtime`` is anything with ``catalog`` and ``cwd``, which is all the
    ``session_id`` and ``path`` domains read off it.

    Raises:
        KeyError: no domain has that name.
        ValueError: the domain needs an object that was not passed.
    """
    found = enumerate_domain(
        domain,
        session=session,
        runtime=runtime,
        scope=scope,
        cursor=cursor,
        query=query,
        limit=limit,
        vocabulary=session.vocabulary,
    )
    return {
        "domain": found.domain,
        "values": [{"value": v.value, "label": v.label} for v in found.values],
        "total": found.total,
    }


def path_completion(text: str, offset: int, cwd: Path) -> dict[str, Any]:
    """The ``@path`` being typed at ``offset`` in ``text``, completed against ``cwd``.

    Returns:
        ``{"completion": None}`` when ``offset`` is in no ``@`` token, else
        ``{"completion": {start, end, token, matches, total}}`` with each match
        ``{name, detail, is_dir}``; ``total`` counts past the match bound.
    """
    from tau_agent_core.attachments import complete_attachment

    completion = complete_attachment(text, offset, cwd=cwd)
    if completion is None:
        return {"completion": None}
    return {
        "completion": {
            "start": completion.start,
            "end": completion.end,
            "token": completion.token,
            "matches": [
                {"name": m.name, "detail": m.detail, "is_dir": m.is_dir} for m in completion.matches
            ],
            "total": completion.total,
        }
    }


def browse_rows(tree: ConversationTree) -> list[dict[str, Any]]:
    """:meth:`ConversationTree.browse`, one flat dict per node, ``is_leaf`` marking the leaf."""
    return [
        {
            "entry_id": node.entry_id,
            "parent_id": node.parent_id,
            "kind": node.kind,
            "role": node.role,
            "preview": node.preview,
            "is_leaf": node.is_cursor,
            "timestamp": node.timestamp,
            "first_kept_id": node.first_kept_id,
            "from_id": node.from_id,
            "is_system": node.is_system,
            "tool_call_ids": list(node.tool_call_ids),
            "tool_call_id": node.tool_call_id,
            "copyable": node.copyable,
            "estimated_tokens": node.estimated_tokens,
        }
        for node in tree.browse()
    ]


MODEL_CATALOG_ATTR = "model_names"
"""The method a model resolver lists its accepted names with."""


def resolver_error_message(exc: KeyError | ValueError) -> str:
    """A model resolver's own message, without the quotes ``KeyError.__str__`` adds.

    A ``KeyError`` with more than one argument has no single message, so its
    ``str`` is what the raiser said.
    """
    if isinstance(exc, KeyError) and len(exc.args) == 1:
        return str(exc.args[0])
    return str(exc)


def model_catalog(resolver: Any) -> list[dict[str, Any]]:
    """Every model name ``resolver`` accepts, each as ``{name, model: {id, provider, context_window}}``.

    Each ``model`` is what resolving the name builds, so it is what ``set_model``
    on that name would install.

    Raises:
        RuntimeError: no resolver, one that cannot list its names, or a name that
            does not build.
    """
    if resolver is None:
        raise RuntimeError(
            "get_models: no model resolver is bound to this AgentSession, so there is "
            "no set of names to enumerate — the frontend binds one at startup "
            "(set_model_resolver, a closure over config 'models'; rpc_mode.py does "
            "this before RPCHandler.run()). set_model would raise here too."
        )
    model_names = getattr(resolver, MODEL_CATALOG_ATTR, None)
    if model_names is None:
        raise RuntimeError(
            f"get_models: the bound model resolver ({type(resolver).__name__}) does not "
            f"declare {MODEL_CATALOG_ATTR}(), so the names it accepts cannot be listed "
            "— refusing rather than answering with an empty catalogue a host would read "
            "as 'set_model has no valid argument' (backends.ConfigModelResolver is the "
            "resolver every shipped frontend binds)"
        )
    listed: list[dict[str, Any]] = []
    for name in model_names():
        try:
            model = resolver(name)
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                f"get_models: config model {name!r} does not build: {resolver_error_message(exc)}"
            ) from exc
        listed.append(
            {
                "name": name,
                "model": {
                    "id": model.id,
                    "provider": model.provider,
                    "context_window": model.context_window,
                },
            }
        )
    return listed


def extension_state(state: LoadExtensionsResult) -> dict[str, Any]:
    """What is loaded and what failed, as ``{extensions, errors}``.

    Returns:
        ``extensions``: ``{name, path, tools, commands, shortcuts, hooks,
        content_hash, subjects}`` per loaded extension; ``errors``: ``{path, error}``
        per file that did not load.
    """
    from tau_agent_core.sdk import summarize_extensions

    return {
        "extensions": [
            {
                "name": info.name,
                "path": info.path,
                "tools": list(info.tools),
                "commands": list(info.commands),
                "shortcuts": list(info.shortcuts),
                "hooks": list(info.hooks),
                "content_hash": info.content_hash,
                "subjects": list(info.subjects),
            }
            for info in summarize_extensions(state)
        ],
        "errors": [{"path": err.path, "error": err.error} for err in state.errors],
    }
