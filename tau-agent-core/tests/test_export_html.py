"""A session tree to one HTML page — ``tau_agent_core.export.render_session_html``.

The entries here are hand-built in the stored camelCase shape, so each test
states the exact tree it renders. docs/HTML-EXPORT.md is the design.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from tau_agent_core.export import ExportConfig, render_session_html

_VOID = {"meta", "img", "br", "hr", "link", "input"}


def _entry(entry_id, parent, second, kind="message", **payload):
    return {
        "type": kind,
        "id": entry_id,
        "parentId": parent,
        "timestamp": f"2026-10-12T10:00:{second:02d}.000Z",
        **payload,
    }


def _msg(entry_id, parent, second, role, content, **fields):
    return _entry(entry_id, parent, second, message={"role": role, "content": content, **fields})


def _text(text):
    return [{"type": "text", "text": text}]


def _forked_tree():
    """s → u1 → a1 → r1 → a2 is the main thread; b1 forks off u1 and was written earlier."""
    return [
        _msg("s", None, 0, "system", _text("you are terse")),
        _msg("u1", "s", 1, "user", _text("list the files")),
        _msg(
            "a1",
            "u1",
            2,
            "assistant",
            [
                {"type": "thinking", "thinking": "use ls"},
                {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
            ],
            model="m",
            stop_reason="toolUse",
        ),
        _msg("b1", "u1", 3, "assistant", _text("ANSWER FROM BRANCH B"), stop_reason="stop"),
        _msg(
            "r1", "a1", 4, "toolResult", _text("a.txt\nb.txt"), tool_call_id="c1", tool_name="bash"
        ),
        _msg("a2", "r1", 5, "assistant", _text("ANSWER ON MAIN"), stop_reason="stop"),
    ]


def _branch_spans(page):
    """The text inside each ``<details class="branch">``, outermost first."""
    spans, depth, start = [], 0, 0
    for match in re.finditer(r'<details class="branch">|<details[ >]|</details>', page):
        token = match.group(0)
        if token == '<details class="branch">':
            if depth == 0:
                start = match.end()
            depth += 1
        elif token == "</details>" and depth:
            depth -= 1
            if depth == 0:
                spans.append(page[start : match.start()])
        elif depth:
            depth += 1
    return spans


class _Balance(HTMLParser):
    """Records every unclosed or mismatched tag, so a broken nesting names itself."""

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1:] or 'nothing'}")
        else:
            self.stack.pop()


def _assert_balanced(page):
    checker = _Balance()
    checker.feed(page)
    assert checker.errors == [] and checker.stack == []


def test_the_default_leaf_is_the_main_thread_and_the_other_child_folds_where_it_forks():
    page = render_session_html(_forked_tree())
    _assert_balanced(page)
    spans = _branch_spans(page)
    assert len(spans) == 1 and "ANSWER FROM BRANCH B" in spans[0]
    assert "ANSWER ON MAIN" not in spans[0]
    assert page.index("list the files") < page.index("ANSWER FROM BRANCH B") < page.index("use ls")
    assert page.index("use ls") < page.index("ANSWER ON MAIN")
    assert page.count("session continues here") == 1
    assert re.search(r'id="e-a2"><header>[^\n]*session continues here', page)


def test_a_named_leaf_swaps_which_child_is_inline():
    page = render_session_html(_forked_tree(), leaf="b1")
    spans = _branch_spans(page)
    assert len(spans) == 1
    assert "ANSWER ON MAIN" in spans[0] and "a.txt" in spans[0]
    assert "ANSWER FROM BRANCH B" not in spans[0]


def test_children_below_the_exported_leaf_fold_rather_than_continue_it():
    page = render_session_html(_forked_tree(), leaf="u1")
    spans = _branch_spans(page)
    assert len(spans) == 2
    assert all("list the files" not in span for span in spans)


def test_every_entry_appears_exactly_once():
    page = render_session_html(_forked_tree())
    for entry_id in ("s", "u1", "a1", "b1", "r1", "a2"):
        assert page.count(f'id="e-{entry_id}"') == 1


@pytest.mark.parametrize(
    "kwargs,match",
    [({"leaf": "nope"}, "names no entry"), ({"config": ExportConfig(format="markdown")}, "html")],
)
def test_bad_arguments_raise(kwargs, match):
    with pytest.raises(ValueError, match=match):
        render_session_html(_forked_tree(), **kwargs)


def test_the_page_can_fetch_nothing():
    """No script, no external reference, a CSP that forbids fetching, images inline."""
    entries = [
        _msg(
            "u", None, 0, "user", [{"type": "image", "data": "iVBORw0K", "mime_type": "image/png"}]
        )
    ]
    page = render_session_html(entries)
    assert "<script" not in page
    assert "default-src 'none'" in page
    assert 'src="data:image/png;base64,iVBORw0K"' in page
    assert not re.search(r'\b(?:src|href)="(?!data:|#)', page)


def test_an_image_with_no_image_mime_type_is_reported_not_embedded():
    entries = [
        _msg("u", None, 0, "user", [{"type": "image", "data": "x", "mime_type": "text/html"}])
    ]
    page = render_session_html(entries)
    assert "<img" not in page
    assert "cannot draw" in page


def test_markup_from_every_source_is_escaped():
    hostile = '<script>alert("x")</script>'
    entries = [
        _msg("u", None, 0, "user", _text(hostile)),
        _msg("b", "u", 1, "assistant", _text(hostile), stop_reason="stop"),
        _msg("a", "u", 2, "toolResult", _text(hostile), tool_call_id=hostile, tool_name=hostile),
    ]
    page = render_session_html(entries, header={"id": hostile, "cwd": hostile})
    assert "<script" not in page
    _assert_balanced(page)


def test_a_deep_linear_conversation_does_not_recurse():
    entries = [_msg("m0", None, 0, "user", _text("start"))]
    for i in range(1, 3000):
        entries.append(_msg(f"m{i}", f"m{i - 1}", i % 60, "user", _text(f"turn {i}")))
    page = render_session_html(entries)
    assert "turn 2999" in page
    assert _branch_spans(page) == []


def test_kinds_nothing_here_knows_are_drawn_raw_and_interrupted_entries_are_marked():
    entries = [
        _msg("u", None, 0, "user", _text("hi")),
        _entry("j", "u", 1, kind="jmfts:document", body="FOREIGN PAYLOAD"),
        {**_msg("a", "j", 2, "assistant", _text("half an ans")), "status": "incomplete"},
    ]
    page = render_session_html(entries)
    assert "jmfts:document" in page and "FOREIGN PAYLOAD" in page
    assert "interrupted" in page


def test_summaries_and_bookkeeping_render_as_their_own_rows():
    entries = [
        _msg("u", None, 0, "user", _text("hi")),
        _entry("n", "u", 1, kind="session_info", name="my session"),
        _entry("c", "n", 2, kind="compaction", summary="WHAT CAME BEFORE", firstKeptId="n"),
        _entry("cfg", "c", 3, kind="customEntry", customType="config", data={"model": "m"}),
    ]
    page = render_session_html(entries)
    assert "<title>my session</title>" in page
    assert "Compaction" in page and "WHAT CAME BEFORE" in page
    assert 'id="e-cfg"' in page


def test_tool_regions_and_reasoning_follow_the_config():
    page = render_session_html(
        _forked_tree(), config=ExportConfig(format="html", include_tool_calls=False)
    )
    assert "a.txt" not in page and '"command"' not in page
    page = render_session_html(
        _forked_tree(), config=ExportConfig(format="html", include_thinking=False)
    )
    assert "use ls" not in page and "a.txt" in page


def test_a_long_tool_result_starts_folded():
    long_output = "\n".join(f"line {i}" for i in range(100))
    entries = [
        _msg("r", None, 0, "toolResult", _text(long_output), tool_call_id="c", tool_name="t")
    ]
    page = render_session_html(entries)
    assert "output · 100 lines" in page


def test_the_header_supplies_identity_and_the_tree_supplies_shape():
    page = render_session_html(
        _forked_tree(), header={"id": "abc123", "cwd": "/work", "parent": None}
    )
    assert "<title>τ session abc123</title>" in page
    assert "/work" in page
    assert "6 in 2 branches" in page


def test_an_empty_log_is_still_a_page():
    page = render_session_html([])
    assert page.startswith("<!DOCTYPE html>") and "0 in 0 branches" in page
