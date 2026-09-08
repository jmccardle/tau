"""The reserved ``customEntry`` an extension uses to stop a session and to ask.

Reference: docs/EXTENSION-LOCKS.md.

Two entry kinds and the algebra over them, and nothing else. Zero imports from
the rest of ``tau_agent_core`` on purpose: ``submission.py`` carries an
:class:`ExtensionRequest` on a refusal, and ``extension_types.py`` builds one, so
anything this module imported would close a cycle between those two.

The lock is read at the CURSOR ONLY — never by walking ancestry, which is what
``agent_spec`` does (``session_log.agent_spec_in_force``). The two differ because
they answer different questions: an ``agent_spec`` is state at a point in the
past, a lock is permission to extend the session now (docs/EXTENSION-LOCKS.md
§2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUEST_ENTRY_TYPE = "extension_request"
"""``customType`` of the entry carrying a lock, an ask, or both (§4)."""

RESPONSE_ENTRY_TYPE = "extension_response"
"""``customType`` of the entry an answered ask appends (§3).

Appending it is what releases the lock, because appending moves the cursor and
the cursor is where the lock is read. Nothing checks that the values are
complete — see §3 on why "fulfilling resolves the lock" is a description of the
normal path rather than a guarantee.
"""


@dataclass(frozen=True)
class ExtensionRequest:
    """One reserved ``customEntry``, read back off the tree.

    Attributes:
        entry_id: The tree entry's own id.
        extension: The appending extension's path, stored rather than looked up
            (§4) — the case this exists for is a reload where that extension
            never loaded.
        sentence: The extension's own one line. Distinct from :attr:`label`,
            which is τ's framing of it.
        lock: Whether a submission at this cursor is refused.
        ask: The validated ask spec a head renders, or ``None``.
        release: A command name that clears the lock, or ``None``. Advisory —
            commands are exempt from the lock by placement (§5), not by name.
    """

    entry_id: str
    extension: str
    sentence: str
    lock: bool
    ask: dict[str, Any] | None
    release: str | None

    @property
    def extension_name(self) -> str:
        """The display stem of :attr:`extension` (``~/x/tectum.py`` → ``tectum``)."""
        return Path(self.extension).stem

    @property
    def label(self) -> str:
        """τ's framing line for this request — the §9 table, as one function.

        Three sentences over the two keys; the ``(False, None)`` combination has
        no label because it draws nothing, and reaching this property with it is
        a construction bug rather than a state.
        """
        name = self.extension_name
        if self.lock and self.ask is not None:
            return f"Extension {name} requires a response"
        if self.lock:
            return f"Extension {name} requires intervention"
        if self.ask is not None:
            return f"Extension {name} requests a response"
        return f"Extension {name} left a note"


def build_request_data(
    extension: str,
    sentence: str,
    *,
    lock: bool = False,
    ask: dict[str, Any] | None = None,
    release: str | None = None,
) -> dict[str, Any]:
    """The ``data`` payload of a request entry, validated (§4).

    Args:
        extension: The appending extension's path. Never fabricated — a caller
            with no extension identity has no business appending one of these.
        sentence: The one line a head shows. Required even for a bare lock,
            because a lock whose owner is absent must still be able to say what
            it is.
        lock: Whether to refuse submissions at this cursor.
        ask: An already-validated ask spec, or ``None``.
        release: The command name that clears the lock, or ``None``.

    Returns:
        The ``data`` dict, carrying only the keys that were set.

    Raises:
        ValueError: an empty ``extension`` or ``sentence``, a non-dict ``ask``,
            an empty ``release``, or all of ``lock``/``ask`` absent — the last
            because an entry that neither locks nor asks is plain
            ``api.append_entry`` bookkeeping and should be spelled that way.
    """
    if not isinstance(extension, str) or not extension:
        raise ValueError("request: 'extension' must be a non-empty path string")
    if not isinstance(sentence, str) or not sentence:
        raise ValueError("request: 'sentence' must be a non-empty string")
    if ask is not None and not isinstance(ask, dict):
        raise ValueError(f"request: 'ask' must be a dict or None, got {type(ask).__name__}")
    if release is not None and (not isinstance(release, str) or not release):
        raise ValueError("request: 'release' must be a non-empty command name or None")
    if not lock and ask is None:
        raise ValueError(
            "request: an entry that neither locks nor asks is durable bookkeeping "
            "with no user-facing behaviour; append it with api.append_entry instead"
        )
    data: dict[str, Any] = {"extension": extension, "sentence": sentence}
    if lock:
        data["lock"] = True
    if ask is not None:
        data["ask"] = ask
    if release is not None:
        data["release"] = release
    return data


def build_response_data(
    request_id: str, extension: str, values: dict[str, Any], action: str
) -> dict[str, Any]:
    """The ``data`` payload of the entry an answered ask appends (§3).

    Args:
        request_id: The request entry this answers.
        extension: The request's own ``extension``, copied so a reader of the
            response alone knows whose it is.
        values: The filled form, keyed by field name.
        action: The action label the user pressed.
    """
    if not request_id:
        raise ValueError("response: 'request_id' is required")
    if not isinstance(values, dict):
        raise ValueError(f"response: 'values' must be a dict, got {type(values).__name__}")
    return {
        "requestId": request_id,
        "extension": extension,
        "action": action,
        "values": values,
    }


def read_request(entry: dict[str, Any]) -> ExtensionRequest | None:
    """One entry as an :class:`ExtensionRequest`, or ``None`` if it is not one.

    Returns ``None`` for every entry that is not a ``customEntry`` of
    :data:`REQUEST_ENTRY_TYPE`, and for one whose ``data`` is malformed — a
    hand-edited or foreign log is read, not enforced against, and a request that
    cannot be understood must not lock a session nobody can unlock.
    """
    if entry.get("type") != "customEntry" or entry.get("customType") != REQUEST_ENTRY_TYPE:
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    extension = data.get("extension")
    sentence = data.get("sentence")
    if not isinstance(extension, str) or not isinstance(sentence, str):
        return None
    ask = data.get("ask")
    release = data.get("release")
    return ExtensionRequest(
        entry_id=str(entry["id"]),
        extension=extension,
        sentence=sentence,
        lock=bool(data.get("lock", False)),
        ask=ask if isinstance(ask, dict) else None,
        release=release if isinstance(release, str) and release else None,
    )


PROVENANCE_ENTRY_TYPES: frozenset[str] = frozenset({"agent_spec"})
"""``customType``s τ writes about itself, which the cursor read looks past.

One member. ``AgentSession.__init__`` appends an ``agent_spec`` node at the end
of construction, so opening a saved session lands the cursor on that node rather
than on whatever the conversation ended with — which would make a lock survive a
restart in the log and not in the read, and the restart case is the one this
whole design exists for.

This is NOT a search for locks (§2, §7). It steps over τ's own provenance writes
and stops at the first entry that is anything else, so a lock spliced mid-path is
still inert and a lock under a user message is still released.
"""


def request_at_cursor(entries: list[dict[str, Any]], cursor: str | None) -> ExtensionRequest | None:
    """The request entry at the cursor, past τ's own provenance nodes, or ``None``.

    The whole read: no ancestry search, and the only entries it steps over are
    :data:`PROVENANCE_ENTRY_TYPES`.

    Args:
        entries: The session log's entries.
        cursor: The resolved cursor id (``SessionLog.cursor``), or ``None``
            pre-root.
    """
    if cursor is None:
        return None
    by_id = {str(e["id"]): e for e in entries if e.get("id") is not None}
    current: str | None = cursor
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)  # cycle guard, mirroring ConversationTree._walk
        entry = by_id.get(current)
        if entry is None:
            return None
        if entry.get("customType") not in PROVENANCE_ENTRY_TYPES:
            return read_request(entry)
        parent = entry.get("parentId")
        current = str(parent) if parent is not None else None
    return None


def find_request(entries: list[dict[str, Any]], entry_id: str) -> ExtensionRequest | None:
    """The request entry with ``entry_id``, wherever it sits, or ``None``.

    For a head answering an ask it has already been shown — not for deciding
    whether the session is locked, which is :func:`request_at_cursor` and only
    that.
    """
    for entry in reversed(entries):
        if str(entry.get("id")) == entry_id:
            return read_request(entry)
    return None


def refusal_reason(request: ExtensionRequest) -> str:
    """The ``rejection_reason`` a locked :class:`SubmissionResult` carries.

    One line naming the owner, its sentence, and the way out it declared, so a
    head with no structured rendering still tells the user something actionable.
    """
    escape = (
        f"Run /{request.release} to clear it."
        if request.release
        else "Answer it, branch to the parent node, or disable the extension."
    )
    sentence = (
        request.sentence if request.sentence.endswith((".", "!", "?")) else request.sentence + "."
    )
    return f"{request.label}: {sentence} {escape}"
