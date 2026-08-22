"""Build a τ :class:`~tau_llm.types.Model` from the models.dev catalog.

τ ships no model list. That is deliberate and stated in
``tau_llm.providers.__init__``: every URL, environment-variable name and context
window in a vendored catalog is a claim τ would have to keep true as vendors move
them, and a stale claim is worse than no claim.

But an operator still has to fill ``context_window``, ``max_tokens``,
``reasoning`` and ``thinking_level_map`` from somewhere, and reading them off a
vendor's documentation by hand is how they end up wrong. models.dev is an
open-source database that already tracks exactly those facts, so this module
reads *that*, at the moment you ask, and prints a config entry you can inspect
before you keep it. Nothing is snapshotted into τ's tree, so nothing in τ goes
stale.

    https://models.dev/api.json — the endpoint
    https://github.com/sst/models.dev — the TOML the endpoint is generated from

models.dev is MIT-licensed, and the conversion of its ``reasoning_options`` into
a thinking-level map follows pi's ``getEffortThinkingLevelMap``
(``packages/ai/scripts/models-dev-reasoning-options.ts``, pi ``5cd93f688``), MIT,
Copyright (c) 2025 Mario Zechner.

## Command line

::

    python -m tau_llm.catalog providers                   # 193 provider ids
    python -m tau_llm.catalog search gpt-5                # matching provider/model pairs
    python -m tau_llm.catalog show openai/gpt-5.1         # the raw catalog record
    python -m tau_llm.catalog config openai/gpt-5.1 \\
        --base-url https://api.openai.com/v1              # a ~/.tau/config.json entry

``config`` writes JSON to stdout and everything else to stderr, so it composes:
redirect it, paste it, or pipe it through ``jq``.

## What models.dev does not carry

**A base URL.** A models.dev provider record holds ``id``, ``name``, ``doc``,
``npm`` and ``env`` — no endpoint. So ``--base-url`` is required and is never
guessed: the same model id is served by a dozen gateways, and picking one for
the operator is how a request goes somewhere they did not intend.

**Anything about wire quirks.** ``compat`` (:mod:`tau_llm.compat`) is inferred
from the endpoint at request time, not from this catalog.

**τ's "max" thinking level.** models.dev reports a ``max`` effort value for
roughly a thousand models. τ's level enum stops at ``xhigh``
(:data:`tau_llm.models.EXTENDED_THINKING_LEVELS`), so a ``max`` value is dropped
rather than folded into ``xhigh`` — silently re-pointing a level at a different
effort is the kind of substitution that makes a config lie. The generated entry
reports the drop; add ``"xhigh": "max"`` to the map by hand if that is what you
want.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

from tau_llm.compat import Compat
from tau_llm.models import EXTENDED_THINKING_LEVELS
from tau_llm.types import Model

MODELS_DEV_API_URL = "https://models.dev/api.json"

# The τ levels a models.dev "effort" value can name, least → most. "off" is
# handled separately (it is models.dev's "none"), so this is
# EXTENDED_THINKING_LEVELS without it.
_EFFORT_LEVELS: tuple[str, ...] = tuple(
    level for level in EXTENDED_THINKING_LEVELS if level != "off"
)


class CatalogError(Exception):
    """The catalog could not answer the question that was asked.

    Raised instead of returning a partially-filled Model: every caller of this
    module is building configuration that a later run will trust, and a
    fabricated context window is not discovered until a turn is truncated.
    """


def fetch_catalog(url: str = MODELS_DEV_API_URL, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch and parse the models.dev catalog.

    Args:
        url: The catalog endpoint. Override to point at a mirror.
        timeout: Whole-request timeout in seconds.

    Returns:
        The parsed catalog: ``{provider_id: {"id", "name", "env", ..., "models": {...}}}``.

    Raises:
        CatalogError: On any transport failure, a non-2xx status, or a body that
            is not a JSON object. httpx is imported here rather than at module
            level so that ``import tau_llm`` stays free of it for a caller who
            only wanted the types.
    """
    import httpx

    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - re-raised with the URL attached
        raise CatalogError(f"Could not fetch the model catalog from {url}: {exc}") from exc

    if not isinstance(payload, dict):
        raise CatalogError(f"{url} returned {type(payload).__name__}, expected a JSON object")
    return payload


def load_catalog(path: str | Path) -> dict[str, Any]:
    """Read a catalog from a local ``api.json`` copy.

    Raises:
        CatalogError: If the file is missing, unreadable, or not a JSON object.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - re-raised with the path attached
        raise CatalogError(f"Could not read the model catalog from {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise CatalogError(f"{path} holds {type(payload).__name__}, expected a JSON object")
    return payload


def iter_models(catalog: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(provider_id, model_id, record)`` for every model in ``catalog``."""
    for provider_id, provider in sorted(catalog.items()):
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, record in sorted(models.items()):
            if isinstance(record, dict):
                yield provider_id, model_id, record


def find_models(catalog: dict[str, Any], pattern: str) -> list[tuple[str, str]]:
    """Every ``(provider_id, model_id)`` whose id or name contains ``pattern``.

    Case-insensitive substring matching. Deliberately not fuzzy: a search that
    invents near-matches makes it harder to tell "this model is not listed" from
    "you spelled it differently".
    """
    needle = pattern.lower()
    hits: list[tuple[str, str]] = []
    for provider_id, model_id, record in iter_models(catalog):
        name = str(record.get("name") or "")
        if needle in model_id.lower() or needle in name.lower() or needle in provider_id.lower():
            hits.append((provider_id, model_id))
    return hits


def get_record(catalog: dict[str, Any], provider_id: str, model_id: str) -> dict[str, Any]:
    """The catalog record for one model.

    Raises:
        CatalogError: If the provider or the model is not listed, naming what is
            available so the caller can correct the spelling.
    """
    provider = catalog.get(provider_id)
    if not isinstance(provider, dict):
        raise CatalogError(
            f"No provider {provider_id!r} in the catalog. "
            f"{len(catalog)} providers are listed; run `providers` to see them."
        )
    models = provider.get("models")
    if not isinstance(models, dict) or model_id not in models:
        count = len(models) if isinstance(models, dict) else 0
        raise CatalogError(
            f"No model {model_id!r} under provider {provider_id!r} "
            f"({count} models listed there; run `search {model_id}` to find it elsewhere)."
        )
    record = models[model_id]
    if not isinstance(record, dict):
        raise CatalogError(f"{provider_id}/{model_id} is not a JSON object in the catalog")
    return record


def thinking_level_map_from_record(
    record: dict[str, Any],
) -> tuple[dict[str, str | dict[str, Any] | None] | None, list[str]]:
    """Convert models.dev ``reasoning_options`` into ``Model.thinking_level_map``.

    Port of pi's ``getEffortThinkingLevelMap``
    (``scripts/models-dev-reasoning-options.ts``), restricted to τ's level enum.

    models.dev describes reasoning three ways, and only one of them converts:

    * ``{"type": "effort", "values": [...]}`` — becomes the map. A value τ has a
      level for maps to itself; a level the model does not offer is set to
      ``None``, which is how ``get_supported_thinking_levels`` marks it
      unsupported; ``"none"`` becomes the ``"off"`` entry.
    * ``{"type": "toggle"}`` — the model reasons on or off, with no effort scale
      and no stated field name. τ's fragment form can express it
      (``{"off": {"chat_template_kwargs": {"enable_thinking": false}}}``), but the
      key differs per server and the catalog does not say which, so guessing one
      would produce a config that is silently ignored.
    * ``{"type": "budget_tokens"}`` — same problem: τ can send a budget fragment,
      but ``thinking_budget_tokens`` is one vendor's spelling of it.

    Returns:
        ``(map_or_None, notes)``. ``notes`` are plain-English remarks for the
        operator — an unconvertible option type, or a dropped ``max`` value —
        and are never silently discarded by the callers in this module.
    """
    options = record.get("reasoning_options")
    notes: list[str] = []
    if not isinstance(options, list) or not options:
        return None, notes

    effort_values: set[str] = set()
    other_types: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type == "effort":
            for value in option.get("values") or []:
                if isinstance(value, str):
                    effort_values.add(value)
        elif isinstance(option_type, str):
            other_types.add(option_type)

    for option_type in sorted(other_types):
        notes.append(
            f"reasoning_options includes {option_type!r}, which has no field name in the "
            "catalog; write a thinking_level_map fragment by hand if you need it"
        )

    if not effort_values:
        return None, notes

    # "default" and JSON null carry no τ level. pi drops them too.
    if "max" in effort_values:
        notes.append(
            "the model offers a 'max' effort that τ has no level for; it is dropped. "
            'Add "xhigh": "max" to the map by hand to reach it.'
        )

    known = effort_values.intersection(_EFFORT_LEVELS)
    if not known and "none" not in effort_values:
        notes.append(
            f"none of the offered effort values {sorted(effort_values)!r} match a τ level; "
            "no thinking_level_map was generated"
        )
        return None, notes

    level_map: dict[str, str | dict[str, Any] | None] = {
        "off": "none" if "none" in effort_values else None
    }
    for level in _EFFORT_LEVELS:
        level_map[level] = level if level in effort_values else None
    return level_map, notes


def model_from_record(
    record: dict[str, Any],
    *,
    base_url: str,
    provider: str,
    api: str = "openai-completions",
    require_tool_call: bool = True,
    compat: Compat | None = None,
) -> tuple[Model, list[str]]:
    """Build a :class:`Model` from one models.dev record.

    Args:
        record: A model record, from :func:`get_record`.
        base_url: The endpoint. Required — models.dev carries no URL, and one
            model id is served by many gateways.
        provider: ``Model.provider``. Usually the models.dev provider id.
        api: The wire protocol. τ implements ``openai-completions`` only today.
        require_tool_call: Refuse a model the catalog marks as unable to call
            tools. τ is a tool-calling harness, so such a model fails on the
            first turn; refusing here says why, months earlier.
        compat: Passed straight through to ``Model.compat``. Not derived from
            the catalog, which carries nothing about wire quirks.

    Returns:
        ``(model, notes)`` — ``notes`` as described on
        :func:`thinking_level_map_from_record`.

    Raises:
        CatalogError: If the record lacks a context window or an output limit,
            or if ``require_tool_call`` is set and the model cannot call tools.
            Neither is defaulted: ``build_model_from_config`` used to hardcode
            128000/4096 for every model in existence, which is the exact class of
            invented number this module exists to replace.
    """
    model_id = str(record.get("id") or "")
    if not model_id:
        raise CatalogError("catalog record has no 'id'")

    if require_tool_call and not record.get("tool_call"):
        raise CatalogError(
            f"{model_id} is listed as unable to call tools, so τ's agent loop cannot use it. "
            "Pass require_tool_call=False (CLI: --allow-no-tools) to build it anyway."
        )

    limit = record.get("limit")
    if not isinstance(limit, dict):
        raise CatalogError(f"{model_id} has no 'limit' block, so its context window is unknown")
    context_window = limit.get("context")
    max_tokens = limit.get("output")
    if not isinstance(context_window, int) or context_window <= 0:
        raise CatalogError(f"{model_id} has no usable limit.context: {context_window!r}")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise CatalogError(f"{model_id} has no usable limit.output: {max_tokens!r}")

    level_map, notes = thinking_level_map_from_record(record)

    return (
        Model(
            id=model_id,
            name=str(record.get("name") or model_id),
            api=api,
            provider=provider,
            base_url=base_url,
            context_window=context_window,
            max_tokens=max_tokens,
            reasoning=bool(record.get("reasoning")),
            thinking_level_map=level_map,
            compat=compat,
        ),
        notes,
    )


def config_entry_from_record(
    record: dict[str, Any],
    *,
    base_url: str,
    provider: str,
) -> tuple[dict[str, Any], list[str]]:
    """A ``~/.tau/config.json`` ``models.<name>`` entry for one catalog record.

    The keys are the ones ``build_model_from_config`` reads, so the output can be
    pasted in as-is. ``Model`` is built first and its fields are read back, which
    keeps this from drifting into a second, looser copy of the same conversion —
    if a value would not survive ``Model`` validation, it never reaches the file.
    """
    model, notes = model_from_record(record, base_url=base_url, provider=provider)
    entry: dict[str, Any] = {
        "backend": model.provider,
        "model": model.id,
        "base_url": model.base_url,
        "context_window": model.context_window,
        "max_tokens": model.max_tokens,
    }
    if model.reasoning:
        entry["reasoning"] = True
    if model.thinking_level_map is not None:
        entry["thinking_level_map"] = model.thinking_level_map
    return entry, notes


def _split_ref(ref: str) -> tuple[str, str]:
    """Split ``provider/model`` — on the FIRST slash only.

    Model ids contain slashes (``moonshotai/kimi-k2.5`` under provider
    ``hpc-ai``), so splitting on the last one would silently address a different
    model.
    """
    provider_id, sep, model_id = ref.partition("/")
    if not sep or not provider_id or not model_id:
        raise CatalogError(f"{ref!r} is not a provider/model reference (e.g. openai/gpt-5.1)")
    return provider_id, model_id


def _load(args: argparse.Namespace) -> dict[str, Any]:
    if args.catalog:
        return load_catalog(args.catalog)
    return fetch_catalog(args.url)


def main(argv: list[str] | None = None) -> int:
    """``python -m tau_llm.catalog``. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tau_llm.catalog",
        description=(
            "Read model facts from models.dev (MIT, https://github.com/sst/models.dev) "
            "and print a τ config entry. Nothing is cached or written."
        ),
    )
    parser.add_argument("--url", default=MODELS_DEV_API_URL, help="catalog endpoint")
    parser.add_argument("--catalog", help="read a local api.json instead of fetching")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("providers", help="list provider ids")

    p_search = sub.add_parser("search", help="find models by substring")
    p_search.add_argument("pattern")

    p_show = sub.add_parser("show", help="print the raw catalog record")
    p_show.add_argument("ref", metavar="PROVIDER/MODEL")

    p_config = sub.add_parser("config", help="print a ~/.tau/config.json models entry")
    p_config.add_argument("ref", metavar="PROVIDER/MODEL")
    p_config.add_argument(
        "--base-url",
        required=True,
        help="the endpoint to talk to; models.dev carries no URL and τ will not guess one",
    )
    p_config.add_argument(
        "--provider",
        help="Model.provider (default: the models.dev provider id)",
    )

    args = parser.parse_args(argv)

    try:
        catalog = _load(args)

        if args.command == "providers":
            for provider_id, provider in sorted(catalog.items()):
                if not isinstance(provider, dict):
                    continue
                env = ", ".join(provider.get("env") or []) or "-"
                count = len(provider.get("models") or {})
                print(f"{provider_id}\t{count} models\t{env}")
            return 0

        if args.command == "search":
            hits = find_models(catalog, args.pattern)
            for provider_id, model_id in hits:
                print(f"{provider_id}/{model_id}")
            if not hits:
                print(f"No model matches {args.pattern!r}.", file=sys.stderr)
                return 1
            return 0

        provider_id, model_id = _split_ref(args.ref)
        record = get_record(catalog, provider_id, model_id)

        if args.command == "show":
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0

        entry, notes = config_entry_from_record(
            record,
            base_url=args.base_url,
            provider=args.provider or provider_id,
        )
        # The env var names live on the PROVIDER record, not the model, and they
        # are the one thing an operator still has to act on after pasting this in.
        env_vars = (catalog.get(provider_id) or {}).get("env") or []
        if env_vars:
            print(f"credential: set {' or '.join(env_vars)}", file=sys.stderr)
        for note in notes:
            print(f"note: {note}", file=sys.stderr)
        print(json.dumps({model_id: entry}, indent=2))
        return 0

    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
