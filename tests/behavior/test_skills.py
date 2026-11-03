"""`skills` (D24 section 6): each prompt template and skill is a command that sends its text as the
cursor's input, and the model is told of the skills at a session's first step. The files are the
user's, under `$TAU_HOME`; the project's are in `tests/config/test_skills.py`."""

from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from world import World, loaded

from tau import (
    Attachments,
    Author,
    Body,
    CommandFailed,
    Cursor,
    Part,
    Skills,
    Stop,
    model_config,
)

DONE = Reply([Part.Text("ok")], Stop.End)


def template(world: World, name: str, text: str) -> None:
    (world.tau / "prompts").mkdir(parents=True, exist_ok=True)
    (world.tau / "prompts" / f"{name}.md").write_text(text, encoding="utf-8")


def skill(world: World, name: str, text: str) -> Path:
    directory = world.tau / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


async def session(
    tmp_path: Path, world: World, *extra: object, tools: bool = False, replies: int = 1
) -> Harness:
    return await Harness.create(
        tmp_path,
        provider=FakeProvider([DONE] * replies),
        extensions=[Skills.of(loaded(world)), *extra],  # type: ignore[list-item]
        tools=[FakeTool("read", "contents")] if tools else [],
    )


def users(h: Harness) -> list[str]:
    return [b.text for b in h.bodies() if isinstance(b, Body.User)]


def prefix(h: Harness) -> list[str]:
    return [b.text for b in h.bodies() if isinstance(b, Body.System)]


# -- templates


@scenario
async def test_a_template_is_a_command_that_sends_its_text_and_the_cursor_answers(
    tmp_path: Path, world: World
) -> None:
    template(world, "review", "---\ndescription: Review a file\n---\nReview $1 for $2\n")
    h = await session(tmp_path, world)
    await h.run("review", {"args": "main.rs bugs"})
    await h.settle()
    assert users(h) == ["Review main.rs for bugs"]
    assert len(h.provider.requests) == 1
    sender = next(n for n in h.nodes() if isinstance(n.body, Body.User)).by
    assert sender == Author.Extension("skills"), "the person only chose the command"
    form = next(f for f in await h.client.commands() if f.qualified == "skills:review")
    assert form.description == "Review a file"
    await h.close()


@scenario
async def test_arguments_is_the_whole_text_and_quotes_group_words_for_numbers(
    tmp_path: Path, world: World
) -> None:
    template(world, "say", "all: $ARGUMENTS\nfirst: $1\nsecond: $2\n")
    h = await session(tmp_path, world)
    await h.run("say", {"args": "'two words' end"})
    await h.settle()
    assert users(h) == ["all: 'two words' end\nfirst: two words\nsecond: end"]
    await h.close()


@scenario
async def test_a_body_with_no_placeholder_gets_the_arguments_after_a_blank_line(
    tmp_path: Path, world: World
) -> None:
    template(world, "plain", "Be brief.\n")
    h = await session(tmp_path, world, replies=2)
    await h.run("plain", {"args": "why is the sky blue"})
    await h.settle()
    await h.run("plain")
    await h.settle()
    assert users(h) == ["Be brief.\n\nwhy is the sky blue", "Be brief."]
    await h.close()


@scenario
async def test_a_missing_number_fails_the_command_and_sends_nothing(
    tmp_path: Path, world: World
) -> None:
    template(world, "two", "$1 and $2")
    h = await session(tmp_path, world)
    with pytest.raises(CommandFailed, match=r"/two uses \$2, but the arguments have 1 word"):
        await h.run("two", {"args": "only"})
    await h.settle()
    assert users(h) == []
    assert h.provider.requests == []
    await h.close()


@scenario
async def test_the_description_defaults_to_the_first_line_of_a_template(
    tmp_path: Path, world: World
) -> None:
    template(world, "first", "\n  Summarize the diff  \nthen list risks\n")
    h = await session(tmp_path, world)
    form = next(f for f in await h.client.commands() if f.qualified == "skills:first")
    assert form.description == "Summarize the diff"
    await h.close()


@scenario
async def test_the_argument_hint_describes_the_argument(tmp_path: Path, world: World) -> None:
    template(world, "hinted", "---\nargument-hint: <file> [focus]\n---\nlook at $ARGUMENTS")
    h = await session(tmp_path, world)
    form = next(f for f in await h.client.commands() if f.qualified == "skills:hinted")
    properties = form.schema["properties"]  # type: ignore[index]
    assert properties["args"]["description"] == "<file> [focus]"  # type: ignore[index]
    await h.close()


@scenario
async def test_what_a_template_sends_passes_the_input_gates_so_attachments_expand_it(
    tmp_path: Path, world: World
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("N", encoding="utf-8")
    template(world, "look", "Read @notes.txt now")
    h = await session(tmp_path, world, Attachments(work))
    await h.run("look")
    await h.settle()
    assert users(h) == ['Read @notes.txt now\n\n<attachment path="notes.txt">\nN\n</attachment>']
    await h.close()


# -- skills


@scenario
async def test_a_skill_sends_its_body_in_a_tag_naming_its_file_then_the_arguments(
    tmp_path: Path, world: World
) -> None:
    path = skill(world, "pdf", "---\ndescription: Work with PDFs\n---\n# PDF\nUse pdftotext.\n")
    h = await session(tmp_path, world)
    await h.run("pdf", {"args": "extract page 2"})
    await h.settle()
    assert users(h) == [
        f'<skill name="pdf" path="{path}">\n# PDF\nUse pdftotext.\n</skill>\n\nextract page 2'
    ]
    await h.close()


@scenario
async def test_a_skill_with_no_arguments_sends_just_the_tag(tmp_path: Path, world: World) -> None:
    path = skill(world, "pdf", "---\ndescription: d\n---\nbody")
    h = await session(tmp_path, world)
    await h.run("pdf")
    await h.settle()
    assert users(h) == [f'<skill name="pdf" path="{path}">\nbody\n</skill>']
    await h.close()


# -- two extensions with one command name


class Ping:
    name = "ping"

    async def review(self, cursor: Cursor) -> str:
        """Another extension's /review."""
        return "pong"

    @property
    def commands(self) -> tuple[object, ...]:
        return (self.review,)


@scenario
async def test_a_generated_command_gives_its_bare_name_to_every_other_command(
    tmp_path: Path, world: World
) -> None:
    template(world, "review", "Review $ARGUMENTS")
    h = await session(tmp_path, world, Ping())
    assert await h.run("review") == "pong", "the bare name is the other extension's"
    await h.settle()
    assert users(h) == []
    await h.run("skills:review", {"args": "x"})
    await h.settle()
    assert users(h) == ["Review x"]
    await h.close()


@scenario
async def test_a_template_named_like_a_built_in_command_does_not_take_its_name(
    tmp_path: Path, world: World
) -> None:
    template(world, "model", "Pick a model for $ARGUMENTS")
    h = await session(tmp_path, world)
    await h.run("model", {"model": "fake"})  # the built-in /model, not the template
    await h.settle()
    assert users(h) == []
    parsed = await h.client.parse_line(h.cursor, "/model fake")
    assert parsed.command.qualified == "tau:model"
    assert "/model" in await h.client.complete_line(h.cursor, "/mod")
    assert "/skills:model" in await h.client.complete_line(h.cursor, "/skills:mo")
    await h.run("skills:model", {"args": "chat"})
    await h.settle()
    assert users(h) == ["Pick a model for chat"]
    await h.close()


# -- the list for the model


@scenario
async def test_the_first_step_writes_one_system_node_listing_the_skills(
    tmp_path: Path, world: World
) -> None:
    path = skill(world, "pdf", "---\ndescription: Work with PDFs & forms\n---\nbody")
    skill(world, "quiet", "---\ndescription: Never listed\ndisable-model-invocation: true\n---\nb")
    template(world, "review", "Review it")
    h = await session(tmp_path, world, tools=True, replies=2)
    await h.send("hello")
    await h.settle()
    listed = prefix(h)[1:]
    assert len(listed) == 1
    assert "read its file with the read tool" in listed[0]
    assert "<name>pdf</name>" in listed[0]
    assert "<description>Work with PDFs &amp; forms</description>" in listed[0]
    assert f"<path>{path}</path>" in listed[0]
    assert "quiet" not in listed[0]
    assert "review" not in listed[0]
    system = next(
        n for n in h.nodes() if isinstance(n.body, Body.System) and n.body.text == listed[0]
    )
    assert system.by == Author.Extension("skills")
    await h.send("again")
    await h.settle()
    assert len(prefix(h)) == 2, "told once, not at every step"
    await h.close()


@scenario
async def test_the_list_comes_before_the_first_message_so_it_is_part_of_the_prefix(
    tmp_path: Path, world: World
) -> None:
    skill(world, "pdf", "---\ndescription: d\n---\nb")
    h = await session(tmp_path, world, tools=True)
    await h.send("hello")
    await h.settle()
    kinds = [type(b).__name__ for b in h.bodies() if not isinstance(b, Body.Config)]
    assert kinds == ["System", "System", "User", "Assistant"]
    await h.close()


@scenario
async def test_without_a_read_tool_nothing_is_written(tmp_path: Path, world: World) -> None:
    skill(world, "pdf", "---\ndescription: d\n---\nb")
    h = await session(tmp_path, world, tools=False)
    await h.send("hello")
    await h.settle()
    assert len(prefix(h)) == 1
    await h.close()


@scenario
async def test_advertise_false_writes_nothing(tmp_path: Path, world: World) -> None:
    skill(world, "pdf", "---\ndescription: d\n---\nb")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([DONE]),
        extensions=[Skills.of(loaded(world))],
        tools=[FakeTool("read", "x")],
        config=model_config("fake", {"skills": {"advertise": False}}),
    )
    await h.send("hello")
    await h.settle()
    assert len(prefix(h)) == 1
    await h.close()


@scenario
async def test_a_session_with_no_skills_writes_nothing_even_with_templates(
    tmp_path: Path, world: World
) -> None:
    template(world, "review", "Review it")
    h = await session(tmp_path, world, tools=True)
    await h.send("hello")
    await h.settle()
    assert len(prefix(h)) == 1
    await h.close()
