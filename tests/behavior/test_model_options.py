"""An extension's section of a model's config (D28 section 7), read through a handle."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fakes import FAKE_MODEL
from harness import Harness, scenario

from tau import ConfigError, Cursor, ModelCatalog, Subject, read_models


@dataclass(frozen=True)
class Section:
    window: int = 4
    label: str = "plain"

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("window must be at least 1")


@dataclass
class Seen:
    sections: list[Section] = field(default_factory=lambda: [])
    refusals: list[str] = field(default_factory=lambda: [])


class Reader:
    """Reads its section of the current model at every step and remembers what it saw."""

    name = "reader"
    model_options = Section

    def __init__(self) -> None:
        self.seen = Seen()

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        try:
            self.seen.sections.append(cursor.model_options())
        except ConfigError as e:
            self.seen.refusals.append(str(e))
        return None


class Bare:
    """An extension that declares no `model_options`."""

    name = "bare"

    def __init__(self) -> None:
        self.seen = Seen()

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        try:
            cursor.model_options()
        except ConfigError as e:
            self.seen.refusals.append(str(e))
        return None


def models(**sections: Mapping[str, object]) -> ModelCatalog:
    settings = {"model": FAKE_MODEL, "provider": "fake", **sections}
    return read_models(cli={"models": {FAKE_MODEL: settings}})


@scenario
async def test_an_absent_section_is_the_type_built_with_no_arguments(tmp_path: Path) -> None:
    reader = Reader()
    h = await Harness.create(tmp_path, extensions=[reader])
    await h.step()
    await h.settle()
    assert reader.seen.sections == [Section()]


@scenario
async def test_the_extension_reads_its_own_section_of_the_models_config(tmp_path: Path) -> None:
    reader = Reader()
    catalog = models(reader={"window": 9}, other={"window": 1})
    h = await Harness.create(tmp_path, extensions=[reader], catalog=catalog)
    await h.step()
    await h.settle()
    assert reader.seen.sections == [Section(window=9, label="plain")]


@scenario
async def test_a_section_its_type_refuses_fails_the_read_naming_the_model(tmp_path: Path) -> None:
    for n, (section, message) in enumerate(
        (({"window": 0}, "window must be at least 1"), ({"size": 1}, "no key ['size']"))
    ):
        reader = Reader()
        h = await Harness.create(
            tmp_path / str(n), extensions=[reader], catalog=models(reader=section)
        )
        await h.step()
        await h.settle()
        assert reader.seen.sections == []
        (refusal,) = reader.seen.refusals
        assert message in refusal
        assert FAKE_MODEL in refusal


@scenario
async def test_an_extension_with_no_model_options_cannot_read_a_section(tmp_path: Path) -> None:
    bare = Bare()
    h = await Harness.create(tmp_path, extensions=[bare], catalog=models(bare={"x": 1}))
    await h.step()
    await h.settle()
    (refusal,) = bare.seen.refusals
    assert "declares no model_options" in refusal
