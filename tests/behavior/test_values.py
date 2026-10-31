"""The type language (D16 section 1): a hint becomes a schema, a check and a conversion."""

from pathlib import Path
from typing import Annotated, Literal, NewType

import pytest
from fakes import FAKE_CONFIG, fake_catalog

from tau_next import CursorId, HintError, LabelName, Model, NodeId, Session
from tau_next.values import Context, Field, check_object, field_of, kind_of, object_schema

CURSOR_ANY = ({"type": "integer"}, {"type": "string", "enum": ["new"]})
NewTypeOfMine = NewType("NewTypeOfMine", str)


def context(session: Session, opened: list[int] | None = None) -> Context:
    """A context whose `"new"` cursor is 101, 102, ... (recorded in `opened`)."""
    log = [] if opened is None else opened

    def new_cursor() -> int:
        log.append(len(log))
        return 101 + len(log) - 1

    return Context(session, fake_catalog(), new_cursor)


def kind(hint: object):
    found = kind_of(hint)
    assert found is not None, hint
    return found


@pytest.mark.parametrize(
    ("hint", "client", "model"),
    [
        (Path, {"type": "string", "format": "path"}, {"type": "string"}),
        (Model, {"type": "string", "format": "model"}, {"type": "string"}),
        (LabelName, {"type": "string", "format": "label"}, {"type": "string"}),
        (NodeId, {"type": "integer", "format": "node"}, {"type": "integer"}),
        (
            CursorId,
            {
                "anyOf": [{"type": "integer"}, {"type": "string", "enum": ["new"]}],
                "format": "cursor",
            },
            {"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["new"]}]},
        ),
        (
            list[Path],
            {"type": "array", "items": {"type": "string", "format": "path"}},
            {"type": "array", "items": {"type": "string"}},
        ),
        (
            Annotated[Model | None, "which model"],
            {
                "anyOf": [{"type": "string", "format": "model"}, {"type": "null"}],
                "description": "which model",
            },
            {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "which model"},
        ),
        (
            Literal["a", "b"],
            {"type": "string", "enum": ["a", "b"]},
            {"type": "string", "enum": ["a", "b"]},
        ),
    ],
)
def test_the_client_schema_names_the_named_types_and_the_model_schema_does_not(
    hint: object, client: dict[str, object], model: dict[str, object]
) -> None:
    found = kind(hint)
    assert found.schema == client
    assert found.model_schema == model


def test_a_parameter_called_format_is_not_mistaken_for_a_schema_key() -> None:
    fields = (field_of("format", Path, None, has_default=False),)
    assert object_schema(fields, model=True)["properties"] == {"format": {"type": "string"}}
    assert object_schema(fields, model=False)["properties"] == {
        "format": {"type": "string", "format": "path"}
    }


def test_what_is_outside_the_language_is_none_and_a_field_of_it_is_a_hint_error() -> None:
    assert kind_of(dict[str, str]) is None
    assert kind_of(NewTypeOfMine) is None
    with pytest.raises(HintError, match="unsupported annotation"):
        field_of("x", dict[str, str], None, has_default=False)
    with pytest.raises(HintError, match="one str"):
        kind_of(Annotated[int, 3])


def test_named_types_are_checked_against_the_session() -> None:
    session = Session.create(FAKE_CONFIG, "sys", 0)
    ctx = context(session)
    (cursor,) = session.cursors()
    session.set_label("main", cursor)
    assert kind(Model).check("fake", ctx) is None
    assert kind(Model).check("nope", ctx) == "must be a configured model, not 'nope'"
    assert kind(CursorId).check(cursor, ctx) is None
    assert kind(CursorId).check("new", ctx) is None
    assert "open cursor" in str(kind(CursorId).check(cursor + 5, ctx))
    assert kind(LabelName).check("main", ctx) is None
    assert kind(LabelName).check("other", ctx) == "must be an existing label, not 'other'"
    assert kind(NodeId).check(0, ctx) is None
    assert "existing node" in str(kind(NodeId).check(len(session.tree), ctx))
    assert kind(NodeId).check(True, ctx) == "must be integer, not boolean"
    session.close()


def test_a_named_type_cannot_be_checked_without_a_session() -> None:
    with pytest.raises(ValueError, match="session"):
        kind(Model).check("fake", None)
    assert kind(Model).needs_session
    assert not kind(Path).needs_session


def test_conversion_makes_paths_and_opens_a_cursor_only_for_new() -> None:
    session = Session.create(FAKE_CONFIG, "sys", 0)
    opened: list[int] = []
    ctx = context(session, opened)
    assert kind(list[Path]).convert(["a", "b/c"], ctx) == [Path("a"), Path("b/c")]
    assert kind(Path | None).convert(None, ctx) is None
    assert kind(CursorId).convert(0, ctx) == 0
    assert opened == []
    assert kind(CursorId).convert("new", ctx) == 101
    assert opened == [0]
    session.close()


def test_a_checked_object_reports_the_first_problem() -> None:
    session = Session.create(FAKE_CONFIG, "sys", 0)
    fields: tuple[Field, ...] = (
        field_of("model", Model, None, has_default=False),
        field_of("n", int, 3, has_default=True),
    )
    ctx = context(session)
    assert check_object(fields, {"model": "fake"}, ctx) is None
    assert check_object(fields, {"model": "nope"}, ctx) == (
        "argument 'model' must be a configured model, not 'nope'"
    )
    assert check_object(fields, {}, ctx) == "missing required arguments ['model']"
    session.close()


def test_a_path_default_is_a_path_and_other_defaults_are_checked_by_shape() -> None:
    assert not field_of("p", Path, Path("x"), has_default=True).required
    with pytest.raises(HintError, match="default 'x'"):
        field_of("p", Path, "x", has_default=True)
    assert not field_of("m", Model, "anything", has_default=True).required
    with pytest.raises(HintError, match=r"default None.*`str \| None`"):
        field_of("s", str, None, has_default=True)
