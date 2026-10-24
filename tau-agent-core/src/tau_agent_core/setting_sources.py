"""Where a turn setting came from: the source vocabulary for model and thinking.

See docs/TURN-SETTINGS.md §8. Every dict on the config path holds JSON: a
source is a plain string under :data:`SOURCE_KEYS`, and whether a resume
restored it is a bool under :data:`RESTORED_KEYS`. :class:`SettingSource` is
the extension-facing view, built only where an extension reads it.
"""

from __future__ import annotations

import inspect
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from tau_llm.docs import agent_facing

Setting = Literal["model", "thinking"]
"""A turn setting that records its source."""

SETTINGS: tuple[Setting, ...] = ("model", "thinking")

SETTING_SOURCES: tuple[str, ...] = ("default", "cli", "command", "rpc", "resume", "spawn")
"""Every source except ``extension:<name>``, which carries the extension's identifier.

- ``default``: config, or the built-in default.
- ``cli``: a ``-m``/``--model`` or ``--thinking`` flag, or an SDK constructor argument.
- ``command``: a head command, such as ``/model`` or ``/thinking``.
- ``rpc``: an RPC or ``tau serve`` verb.
- ``resume``: restored by a resume from an entry that recorded no source.
- ``spawn``: the model a sub-agent or comparison cursor was opened with.
"""

EXTENSION_SOURCE_PREFIX = "extension:"

SOURCE_KEYS: dict[Setting, str] = {"model": "model_source", "thinking": "thinking_source"}
"""The config key that records each setting's source."""

RESTORED_KEYS: dict[Setting, str] = {"model": "model_restored", "thinking": "thinking_restored"}
"""The head model-config key that says a resume restored each setting; never in the tree."""

ACTING_EXTENSION: ContextVar[str | None] = ContextVar("tau_acting_extension", default=None)
"""The identifier of the extension whose code is running, while one is."""


def check_source(source: object) -> str:
    """Return ``source`` if it is in the vocabulary.

    Raises:
        ValueError: it is neither a :data:`SETTING_SOURCES` value nor
            ``extension:<name>`` with a non-empty name.
    """
    if not isinstance(source, str):
        raise ValueError(f"{source!r} is not a setting source; a source is a string")
    if source in SETTING_SOURCES:
        return source
    if source.startswith(EXTENSION_SOURCE_PREFIX) and len(source) > len(EXTENSION_SOURCE_PREFIX):
        return source
    raise ValueError(
        f"{source!r} is not a setting source; expected one of "
        f"{', '.join(SETTING_SOURCES)} or 'extension:<name>'"
    )


def check_setting(setting: str) -> Setting:
    """Return ``setting`` if it records a source.

    Raises:
        ValueError: it is not ``"model"`` or ``"thinking"``.
    """
    if setting == "model":
        return "model"
    if setting == "thinking":
        return "thinking"
    raise ValueError(f"{setting!r} is not a turn setting; expected 'model' or 'thinking'")


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class SettingSource:
    """Where the model or thinking level in force came from, as an extension reads it.

    Never stored on the config path, which holds the JSON keys of :func:`source_keys`.

    Attributes:
        source: A :data:`SETTING_SOURCES` value or ``extension:<name>``.
        restored: Whether a resume restored the value from the tree. ``source``
            is then the one recorded with it, and ``resume`` when the entry
            predates sources.
    """

    source: str
    restored: bool = False

    def __post_init__(self) -> None:
        check_source(self.source)


@agent_facing(topic="sessions")
def source_keys(setting: str, source: str, *, restored: bool = False) -> dict[str, Any]:
    """The JSON keys a head's model config carries for ``setting``'s source.

    Args:
        setting: ``"model"`` or ``"thinking"``.
        source: A :data:`SETTING_SOURCES` value or ``extension:<name>``.
        restored: Whether a resume restored the value.

    Returns:
        ``{"<setting>_source": source, "<setting>_restored": restored}``.

    Raises:
        ValueError: ``setting`` or ``source`` is outside the vocabulary.
    """
    checked = check_setting(setting)
    return {SOURCE_KEYS[checked]: check_source(source), RESTORED_KEYS[checked]: restored}


@agent_facing(topic="sessions")
def restored_keys(recorded: Mapping[str, Any], setting: str) -> dict[str, Any]:
    """:func:`source_keys` for a value a resume restores from ``recorded``.

    Args:
        recorded: The folded config at the resumed position (``config_at``).
        setting: ``"model"`` or ``"thinking"``.

    Returns:
        The recorded source, or ``resume`` when none was recorded, with
        ``restored`` true.

    Raises:
        ValueError: ``setting`` is not a turn setting, or the recorded source is
            not in the vocabulary.
    """
    value = recorded.get(SOURCE_KEYS[check_setting(setting)])
    return source_keys(setting, "resume" if value is None else value, restored=True)


def acting_extension_source(what: str) -> str:
    """``extension:<name>`` for the extension whose code is running.

    Args:
        what: The call being attributed, for the error.

    Raises:
        RuntimeError: no extension's dispatch is running, so the change has no
            nameable source.
    """
    name = ACTING_EXTENSION.get()
    if name is None:
        raise RuntimeError(
            f"{what}: called outside any extension's hook, command or tool, so the "
            "change cannot be attributed to an extension (docs/TURN-SETTINGS.md §8)"
        )
    return EXTENSION_SOURCE_PREFIX + name


def as_extension(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """``fn`` wrapped to run, and finish if it returns an awaitable, as extension ``name``.

    Args:
        name: The extension's identifier.
        fn: A handler, sync or async.
    """

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        token = ACTING_EXTENSION.set(name)
        try:
            result = fn(*args, **kwargs)
        finally:
            ACTING_EXTENSION.reset(token)
        if not inspect.isawaitable(result):
            return result

        async def finish() -> Any:
            inner = ACTING_EXTENSION.set(name)
            try:
                return await result
            finally:
                ACTING_EXTENSION.reset(inner)

        return finish()

    return wrapped
