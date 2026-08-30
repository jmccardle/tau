"""``@file`` attachments: the decision that a word in a prompt names a file.

Reference: docs/FILE-ATTACHMENTS.md.

This module is the ``commands.py`` of file references, and it is deliberately the
same shape: pure functions that take the text plus a working directory and return
a typed answer, so a Textual editor, a headless run, an RPC client and a test all
get the same answer from the same code. Nothing here touches a session, an event
bus or a model.

Three questions, three functions:

- :func:`scan_attachments` — which words in this text name files, and what would
  happen to each. It stats (and reads, for a small file) but produces no prompt
  text, because the chat editor calls it on every keystroke to draw the
  attachment bar.
- :func:`render_attachments` — turn those into the block prefix and the image
  content blocks that go on the submission. Called once, at submit time.
- :func:`complete_attachment` — the candidate paths for a half-typed ``@…``,
  for the editor's Tab cycle.

The block vocabulary is two words, and the difference between them is whether the
content is present:

- ``<attachment filename="notes.txt">`` … ``</attachment>`` — the content IS
  here, inline. An image attachment has an empty body; its pixels ride as an
  ``ImageContent`` block on the same message.
- ``<reference filename="big.log" path="/abs/big.log" size="4.2 MB" reason="…" />``
  — the content is NOT here. The model is given the path, the size and the reason
  so it can decide to ``read`` the file itself.

A file that cannot be read is a ``<reference … error="…">``, never a silently
dropped word: the model is told the attachment failed, and
:attr:`RenderedAttachments.failures` says the same thing to the frontend so it can
tell the human. What is NOT an error is a ``@word`` that names nothing — that is
ordinary prose and goes to the model as typed, exactly as an unrecognised ``/…``
does (:func:`~tau_agent_core.commands.resolve_command`).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from tau_llm.docs import agent_facing

from tau_agent_core.tools.image_resize import (
    DEFAULT_MAX_IMAGE_DIMENSION,
    ImageSupportUnavailable,
    resize_image,
)

#: A file reference: ``@`` at the start of the text or after whitespace, followed
#: by at least one non-space character. The same rule the CLI has always used for
#: a positional ``@file`` argument (``headless.assemble_prompt``), extended to
#: find references inside a line rather than only as a whole argument.
#:
#: An e-mail address in prose (``ask bob@example.com``) does not match, because
#: the ``@`` there is not preceded by whitespace.
ATTACHMENT_PATTERN = re.compile(r"(?:(?<=\s)|\A)@(\S+)")

#: Trailing characters that are punctuation of the sentence rather than part of
#: the path. Stripped ONLY when the literal token names nothing and the stripped
#: one names a real file, so ``@notes.txt,`` in prose attaches ``notes.txt`` and a
#: file genuinely called ``odd,`` still resolves to itself. This is the "suggest
#: the corrected value" half of Fail-Early, not a fallback: nothing is fabricated,
#: and a miss stays a miss.
TRAILING_PUNCTUATION = ",.;:!?)]}'\"`"

#: The inline budget. A text file larger than this becomes a ``<reference>``
#: instead of being pasted into the prompt. 10 KB is roughly 2500 tokens — big
#: enough for a config file, a stack trace or a short module, small enough that
#: attaching three of them does not rewrite the turn's context.
DEFAULT_INLINE_LIMIT = 10 * 1024

#: Extension → mime type for the formats a vision model is sent. The same set
#: ``read`` supports (``tools/read.py``, ``IMAGE_EXTENSIONS``), spelled as the
#: mapping this module needs; a divergence between the two would mean ``@shot.png``
#: and ``read("shot.png")`` disagreeing about what an image is.
IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: What :func:`scan_attachments` decided one reference is.
#:
#: - ``"inline"`` — a UTF-8 text file within the inline budget; its content is
#:   pasted into an ``<attachment>`` block.
#: - ``"image"`` — a supported image format; it becomes an ``ImageContent`` block
#:   plus an empty ``<attachment>`` naming it.
#: - ``"reference"`` — it exists but its content is not being sent: too large,
#:   not UTF-8, or unreadable. It becomes a ``<reference>`` block.
#: - ``"unresolved"`` — the word names no file (or names a directory). It is
#:   prose; nothing is attached and nothing is reported.
AttachmentKind = Literal["inline", "image", "reference", "unresolved"]

#: The kinds that produce a block and therefore belong in a frontend's
#: attachment list. ``"unresolved"`` is prose and is deliberately absent.
SENDABLE_KINDS: tuple[AttachmentKind, ...] = ("inline", "image", "reference")

# Rows a completion list returns at most. The popup shows fewer at a time; this
# bounds the LISTING, so ``@`` in a directory of 40000 files costs one bounded
# scandir rather than 40000 rows nobody will read.
_COMPLETION_LIMIT = 200


@agent_facing(topic="messages")
@dataclass(frozen=True)
class Attachment:
    """One ``@file`` reference found in a prompt, and what will become of it.

    Frozen, and carrying its own span, because a frontend uses it for two things
    at once: drawing a row that says what is attached, and editing the text that
    produced it when the human removes that row (:func:`remove_attachment`).

    Attributes:
        token: The reference as typed, without the ``@``. Relative paths stay
            relative — this is what the ``filename`` attribute of the emitted
            block says, so the model sees the name the human used.
        start: Index of the ``@`` in the text this was scanned from.
        end: Index one past the last character of the reference. ``text[start:end]``
            is ``"@" + token``, which :func:`remove_attachment` checks before it
            cuts.
        kind: What will be sent. See :data:`AttachmentKind`.
        path: The resolved absolute path, or ``None`` when ``kind`` is
            ``"unresolved"``.
        size: Size on disk in bytes. 0 when unresolved.
        mime_type: The image mime type; ``""`` for everything else.
        note: Why this is a ``"reference"`` rather than inline, or why it is
            unresolved. ``""`` when there is nothing to explain. It is shown to
            the human AND written into the block, because a model told only "the
            content is missing" cannot tell a 4 MB file from an unreadable one.
    """

    token: str
    start: int
    end: int
    kind: AttachmentKind
    path: Path | None = None
    size: int = 0
    mime_type: str = ""
    note: str = ""


@agent_facing(topic="messages")
@dataclass(frozen=True)
class RenderedAttachments:
    """The prompt prefix and image blocks a set of attachments produced.

    Attributes:
        prefix: The ``<attachment>``/``<reference>`` blocks, in the order the
            references appeared, each ending in a newline. Prepended to the user's
            own text — the human's words stay last, where the model reads them as
            the instruction rather than as a caption on the final file.
        images: ``ImageContent``-shaped block dicts (``{"type": "image", "data":
            <base64>, "mime_type": …}``) to put on ``Submission.images``.
        failures: One human-readable line per attachment that could not be sent as
            intended. Empty when everything worked. The frontend shows these; the
            corresponding block already says the same thing to the model.
    """

    prefix: str
    images: tuple[dict[str, Any], ...]
    failures: tuple[str, ...]


@agent_facing(topic="messages")
@dataclass(frozen=True)
class PathCompletion:
    """One candidate path for a half-typed ``@…``.

    Attributes:
        name: What replaces the token after the ``@`` — the whole path as it
            would be typed, not just the last segment, so inserting it is a
            single span replacement. Directories end in ``/``.
        detail: A short right-hand column for the popup: a human size for a file,
            ``"dir"`` for a directory.
        is_dir: Whether this candidate is a directory. A directory is inserted
            without a trailing space, because the next thing the human wants is
            to keep completing into it.
    """

    name: str
    detail: str
    is_dir: bool


@agent_facing(topic="messages")
@dataclass(frozen=True)
class AttachmentCompletions:
    """The candidate paths for the ``@…`` the cursor is inside.

    The same shape as :class:`~tau_agent_core.commands.CommandCompletions`, and
    for the same reason: an empty ``matches`` is not "nothing to say", it is the
    warning that this ``@…`` names no file and will be sent as ordinary text.

    Attributes:
        start: Index of the ``@`` in the text.
        end: Index one past the token, i.e. the end of the span a completion
            replaces. The whole token is replaced even when the cursor sits in
            the middle of it — one rule, so what Tab does is predictable.
        token: The reference as typed so far, without the ``@``.
        matches: The candidates, alphabetical, at most :data:`_COMPLETION_LIMIT`.
        total: How many candidates matched before that cap, so a frontend can say
            that the list is not all of them.
    """

    start: int
    end: int
    token: str
    matches: tuple[PathCompletion, ...]
    total: int


def human_size(size: int) -> str:
    """Format a byte count for a human: ``"812 bytes"``, ``"9.8 KB"``, ``"4.2 MB"``.

    Args:
        size: A byte count.

    Returns:
        The count with a unit. Kilobytes are 1024 bytes, because this number is
        compared against :data:`DEFAULT_INLINE_LIMIT`, which is also binary.
    """
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _resolve(token: str, base: Path) -> tuple[str, Path | None]:
    """Resolve one reference to a path, retrying without trailing punctuation.

    Returns the token as it should be recorded (which is shorter than the input
    when the punctuation retry succeeded) and the resolved path, or ``None`` when
    nothing exists at either spelling.
    """
    for candidate in _candidate_tokens(token):
        expanded = Path(candidate).expanduser()
        path = expanded if expanded.is_absolute() else base / expanded
        try:
            if path.exists():
                return candidate, path
        except OSError:
            # A path too long for the filesystem, or a broken mount. It names no
            # readable file, which is the same answer as "does not exist".
            continue
    return token, None


def _candidate_tokens(token: str) -> list[str]:
    """The spellings of ``token`` to try, most literal first."""
    trimmed = token.rstrip(TRAILING_PUNCTUATION)
    if trimmed and trimmed != token:
        return [token, trimmed]
    return [token]


def _classify(path: Path, inline_limit: int) -> tuple[AttachmentKind, int, str, str]:
    """Decide what one existing file becomes. Returns ``(kind, size, mime, note)``."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "reference", 0, "", f"cannot stat: {exc.strerror or exc}"

    mime = IMAGE_MIME_TYPES.get(path.suffix.lower(), "")
    if mime:
        return "image", size, mime, ""

    if size > inline_limit:
        return (
            "reference",
            size,
            "",
            f"{human_size(size)}, over the {human_size(inline_limit)} inline limit",
        )

    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return "reference", size, "", "not UTF-8 text"
    except OSError as exc:
        return "reference", size, "", f"cannot read: {exc.strerror or exc}"

    return "inline", size, "", ""


@agent_facing(topic="messages")
def scan_attachments(
    text: str,
    *,
    cwd: Path | None = None,
    inline_limit: int = DEFAULT_INLINE_LIMIT,
) -> tuple[Attachment, ...]:
    """Find the ``@file`` references in ``text`` and say what each one is.

    Cheap enough to call on every keystroke, which is what the chat editor does:
    it stats every reference and reads only the files that are within
    ``inline_limit``, because deciding "text or binary" has no answer that does
    not look at the bytes. It reads nothing over the limit and returns no file
    contents — :func:`render_attachments` reads again at submit time, so what is
    sent is the file as it stood when the human pressed Enter.

    A reference that names nothing is returned with ``kind="unresolved"`` rather
    than dropped, so a caller that wants to say so can. Nothing here raises: an
    unreadable file is a ``"reference"`` carrying the reason.

    Args:
        text: The prompt as typed.
        cwd: The directory relative references resolve against. Defaults to the
            process working directory.
        inline_limit: The largest file, in bytes, whose content is pasted into
            the prompt. Larger files become ``<reference>`` blocks.

    Returns:
        One :class:`Attachment` per reference, in the order they appear in
        ``text``.
    """
    base = Path.cwd() if cwd is None else cwd
    found: list[Attachment] = []
    for match in ATTACHMENT_PATTERN.finditer(text):
        token, path = _resolve(match.group(1), base)
        start = match.start()
        end = start + 1 + len(token)
        if path is None:
            found.append(
                Attachment(
                    token=token, start=start, end=end, kind="unresolved", note="no such file"
                )
            )
            continue
        if path.is_dir():
            found.append(
                Attachment(
                    token=token,
                    start=start,
                    end=end,
                    kind="unresolved",
                    path=path,
                    note="is a directory",
                )
            )
            continue
        kind, size, mime, note = _classify(path, inline_limit)
        found.append(
            Attachment(
                token=token,
                start=start,
                end=end,
                kind=kind,
                path=path,
                size=size,
                mime_type=mime,
                note=note,
            )
        )
    return tuple(found)


def _escape_attribute(value: str) -> str:
    """Escape a value for an XML-style attribute in a block header.

    Only the three characters that would end the attribute or the tag. The block
    BODY is never escaped: a Python file full of ``<`` and ``&`` must reach the
    model as itself, and these blocks are a framing convention for a language
    model, not a document an XML parser will see.
    """
    return value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _reference_block(attachment: Attachment, *, error: str = "") -> str:
    """One ``<reference … />`` line: the path and size, in place of the content."""
    parts = [f'filename="{_escape_attribute(attachment.token)}"']
    if attachment.path is not None:
        parts.append(f'path="{_escape_attribute(str(attachment.path))}"')
    parts.append(f'size="{human_size(attachment.size)}"')
    reason = error or attachment.note
    if reason:
        key = "error" if error else "reason"
        parts.append(f'{key}="{_escape_attribute(reason)}"')
    return f"<reference {' '.join(parts)} />\n"


@agent_facing(topic="messages")
def render_attachments(
    attachments: Sequence[Attachment],
    *,
    max_image_dimension: int = DEFAULT_MAX_IMAGE_DIMENSION,
) -> RenderedAttachments:
    """Build the prompt prefix and the image blocks for ``attachments``.

    Reads each file again — :func:`scan_attachments` deliberately keeps no
    content — so what goes to the model is the file as it stands now, and a file
    that has been deleted or chmod-ed since the human typed the ``@`` is reported
    rather than sent as stale bytes.

    Images are bounded by :func:`~tau_agent_core.tools.image_resize.resize_image`
    before they are encoded. Pillow missing is NOT a reason to send the image
    unresized (see that module): the attachment degrades to a ``<reference>``
    naming the extra to install, and the frontend is told through ``failures``.

    This is CPU-bound for a large image, because bounding one decodes it.

    Args:
        attachments: What :func:`scan_attachments` returned. ``"unresolved"``
            entries are skipped — they are prose, and the human's own text
            already contains them.
        max_image_dimension: The largest width or height, in pixels, an attached
            image may have. Larger images are scaled down.

    Returns:
        A :class:`RenderedAttachments`.
    """
    blocks: list[str] = []
    images: list[dict[str, Any]] = []
    failures: list[str] = []

    for attachment in attachments:
        if attachment.kind == "unresolved" or attachment.path is None:
            continue
        name = _escape_attribute(attachment.token)

        if attachment.kind == "reference":
            blocks.append(_reference_block(attachment))
            continue

        if attachment.kind == "image":
            try:
                bounded = resize_image(
                    attachment.path.read_bytes(),
                    attachment.mime_type,
                    max_image_dimension,
                )
            except ImageSupportUnavailable as exc:
                blocks.append(_reference_block(attachment, error=str(exc)))
                failures.append(f"{attachment.token}: {exc}")
                continue
            except OSError as exc:
                blocks.append(_reference_block(attachment, error=str(exc)))
                failures.append(f"{attachment.token}: {exc}")
                continue
            images.append(
                {
                    "type": "image",
                    "data": base64.b64encode(bounded.data).decode("ascii"),
                    "mime_type": bounded.mime_type,
                }
            )
            # An empty body, because the body is the image block above. The
            # filename is what makes two attached screenshots distinguishable to
            # the model, which the image blocks alone cannot be.
            detail = f'type="{_escape_attribute(bounded.mime_type)}"'
            if bounded.resized:
                w, h = bounded.size
                ow, oh = bounded.original_size
                detail += f' resized="{ow}x{oh} to {w}x{h}"'
            blocks.append(f'<attachment filename="{name}" {detail} />\n')
            continue

        try:
            content = attachment.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            blocks.append(_reference_block(attachment, error=str(exc)))
            failures.append(f"{attachment.token}: {exc}")
            continue
        body = content if content.endswith("\n") else content + "\n"
        blocks.append(f'<attachment filename="{name}">\n{body}</attachment>\n')

    return RenderedAttachments(
        prefix="".join(blocks), images=tuple(images), failures=tuple(failures)
    )


@agent_facing(topic="messages")
def remove_attachment(text: str, attachment: Attachment) -> str:
    """Delete one ``@file`` reference from ``text``.

    What "remove this attachment" means for a reference typed into a prompt: the
    ``@…`` word goes away, because the word IS the attachment. The separator it
    leaves behind is collapsed, so removing the middle of ``look at @a.py and
    @b.py`` does not leave a double space.

    Args:
        text: The text the attachment was scanned from.
        attachment: The reference to remove.

    Returns:
        ``text`` without the reference.

    Raises:
        ValueError: ``text`` no longer holds that reference at that span. The
            caller's text has changed since the scan, and cutting the recorded
            span would delete something else (Fail-Early).
    """
    span = text[attachment.start : attachment.end]
    if span != f"@{attachment.token}":
        raise ValueError(
            f"the text no longer holds @{attachment.token} at {attachment.start}:"
            f"{attachment.end} (found {span!r}) — it was edited after the scan"
        )
    before = text[: attachment.start]
    after = text[attachment.end :]
    if before.endswith(" ") and after.startswith(" "):
        after = after[1:]
    elif not after:
        before = before.rstrip(" ")
    elif not before and after.startswith(" "):
        after = after[1:]
    return before + after


def _completion_span(text: str, cursor: int) -> tuple[int, int] | None:
    """The span of the ``@…`` the cursor is inside, or ``None``.

    "Inside" includes sitting immediately after the last character, which is
    where the cursor is when someone types a prefix and reaches for Tab. The span
    covers the WHOLE token, not just the part before the cursor.
    """
    for match in ATTACHMENT_PATTERN.finditer(text):
        if match.start() <= cursor <= match.end():
            return match.start(), match.end()
    # A bare "@" matches no token (the pattern needs one non-space character), so
    # it is handled here: it is a reference with an empty prefix, which lists the
    # working directory.
    if cursor > 0 and text[cursor - 1] == "@" and (cursor == 1 or text[cursor - 2].isspace()):
        return cursor - 1, cursor
    return None


@agent_facing(topic="messages")
def complete_attachment(
    text: str,
    cursor: int,
    *,
    cwd: Path | None = None,
) -> AttachmentCompletions | None:
    """Candidate paths for the ``@…`` the cursor is inside. ``None`` for "not one".

    Pure in the same sense as :func:`~tau_agent_core.commands.complete_command`:
    it reads the filesystem but decides nothing and runs nothing, so an editor, a
    test and another frontend all get the same list.

    Matching is a case-sensitive prefix test on the last path segment, which is
    what a shell does. Hidden entries are offered only once the prefix itself
    starts with a dot, so ``@`` in a home directory does not open with forty
    dotfiles.

    Args:
        text: The editor's contents.
        cursor: The cursor's character offset into ``text``.
        cwd: The directory relative references resolve against. Defaults to the
            process working directory.

    Returns:
        An :class:`AttachmentCompletions` when the cursor is inside a ``@…``,
        with an empty ``matches`` when nothing matches — that emptiness is the
        "this names no file" warning, not an absence of information. ``None`` when
        the cursor is not inside a reference at all.
    """
    span = _completion_span(text, cursor)
    if span is None:
        return None
    start, end = span
    token = text[start + 1 : end]

    base = Path.cwd() if cwd is None else cwd
    directory, _, stem = token.rpartition("/")
    root = Path(directory).expanduser() if directory else Path()
    search = root if root.is_absolute() else base / root

    try:
        entries = sorted(search.iterdir(), key=lambda p: p.name)
    except OSError:
        # The directory half of the token names nothing yet — the human is still
        # typing it. No candidates, which the caller renders as the warning.
        return AttachmentCompletions(start=start, end=end, token=token, matches=(), total=0)

    prefix = f"{directory}/" if directory else ""
    matches: list[PathCompletion] = []
    total = 0
    for entry in entries:
        if not entry.name.startswith(stem):
            continue
        if entry.name.startswith(".") and not stem.startswith("."):
            continue
        total += 1
        if len(matches) >= _COMPLETION_LIMIT:
            continue
        is_dir = entry.is_dir()
        try:
            detail = "dir" if is_dir else human_size(entry.stat().st_size)
        except OSError:
            detail = ""
        matches.append(
            PathCompletion(
                name=f"{prefix}{entry.name}/" if is_dir else f"{prefix}{entry.name}",
                detail=detail,
                is_dir=is_dir,
            )
        )

    return AttachmentCompletions(
        start=start, end=end, token=token, matches=tuple(matches), total=total
    )


#: Matches one whole ``<attachment …>…</attachment>`` block, capturing the header
#: attributes and the body. Non-greedy so two blocks in a row stay two blocks, and
#: the header may not END in ``/`` so a self-closing image block is not mistaken
#: for the opening tag of the next inlined file.
_ATTACHMENT_BLOCK = re.compile(
    r"<attachment ([^>]*[^/>])>\n(.*?)</attachment>\n",
    re.DOTALL,
)


@agent_facing(topic="messages")
def elide_attachment_bodies(text: str) -> str:
    """Replace inlined attachment bodies with a one-line summary, for display.

    A transcript is a conversation, and a 10 KB file pasted into a user bubble
    pushes the conversation off the screen. This is the DISPLAY transform for
    that: the block keeps its header and its shape, and its body becomes a
    visible marker saying how much was elided.

    It is deliberately not a lossy record. What was sent is on the wire and in
    the session log, unchanged; this is what the frontend draws. The marker says
    so, rather than leaving a shortened body that reads as the whole file.

    Args:
        text: A prompt that may contain ``<attachment>`` blocks.

    Returns:
        The same text with each inlined body replaced by a summary line. Empty
        (image) attachment blocks are self-closing and are untouched.
    """

    def _fold(match: re.Match[str]) -> str:
        header, body = match.group(1), match.group(2)
        lines = body.count("\n")
        unit = "line" if lines == 1 else "lines"
        size = human_size(len(body.encode("utf-8")))
        return f"<attachment {header}>\n  … {lines} {unit}, {size} not shown …\n</attachment>\n"

    return _ATTACHMENT_BLOCK.sub(_fold, text)
