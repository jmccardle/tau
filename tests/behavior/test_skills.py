"""`skills` (D24 section 6): each prompt template and skill is a command that sends its text as the
cursor's input, and the model is told of the skills at a session's first step. The files are the
user's, under `$TAU_HOME`; the project's are in `tests/config/test_skills.py`."""

from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from world import World, loaded, tau, trust

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
    await h.run("say", {"args": '"two words" end'})
    await h.settle()
    assert users(h) == ['all: "two words" end\nfirst: two words\nsecond: end']
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
async def test_a_missing_number_is_empty_text(tmp_path: Path, world: World) -> None:
    template(world, "two", "$1 and $2.")
    h = await session(tmp_path, world)
    await h.run("two", {"args": "only"})
    await h.settle()
    assert users(h) == ["only and ."]
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
async def test_what_a_template_sends_passes_the_input_gates_but_is_not_a_typed_input(
    tmp_path: Path, world: World
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("N", encoding="utf-8")
    template(world, "look", "Read @notes.txt now")
    h = await session(tmp_path, world, Attachments(work))
    await h.run("look")
    await h.settle()
    assert users(h) == ["Read @notes.txt now"], "attachments expand only what a person typed"
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
        f'<skill name="pdf" path="{path}">\n# PDF\nUse pdftotext.\n</skill>\n'
        f"A relative path in a skill starts from the directory of its file: {path.parent}.\n\n"
        "extract page 2"
    ]
    await h.close()


@scenario
async def test_a_skill_with_no_arguments_sends_just_the_tag(tmp_path: Path, world: World) -> None:
    path = skill(world, "pdf", "---\ndescription: d\n---\nbody")
    h = await session(tmp_path, world)
    await h.run("pdf")
    await h.settle()
    assert users(h) == [
        f'<skill name="pdf" path="{path}">\nbody\n</skill>\n'
        f"A relative path in a skill starts from the directory of its file: {path.parent}."
    ]
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


# -- what the arguments of a template are


@scenario
async def test_a_single_quote_is_an_apostrophe_and_double_quotes_group(
    tmp_path: Path, world: World
) -> None:
    template(world, "two", "1=$1 2=$2")
    h = await session(tmp_path, world, replies=2)
    await h.run("two", {"args": 'don\'t "stop now"'})
    await h.settle()
    await h.run("two", {"args": '"" x'})
    await h.settle()
    assert users(h) == ["1=don't 2=stop now", "1= 2=x"]
    await h.close()


@scenario
async def test_two_dollars_write_one_and_numbers_past_nine_are_numbers(
    tmp_path: Path, world: World
) -> None:
    template(world, "cost", "$$5 for $10, not $1 0 or $$1")
    h = await session(tmp_path, world)
    await h.run("cost", {"args": "a b c d e f g h i j"})
    await h.settle()
    assert users(h) == ["$5 for j, not a 0 or $1"]
    await h.close()


@scenario
async def test_a_body_with_only_dollar_signs_still_gets_the_arguments(
    tmp_path: Path, world: World
) -> None:
    template(world, "price", "Costs $$5.")
    h = await session(tmp_path, world)
    await h.run("price", {"args": "why"})
    await h.settle()
    assert users(h) == ["Costs $5.\n\nwhy"]
    await h.close()


@scenario
async def test_an_argument_error_is_said_once_with_the_usage_and_ends_nothing_wrong(
    tmp_path: Path, world: World
) -> None:
    template(world, "review", "---\nargument-hint: <file> [focus]\n---\nReview $1")
    h = await session(tmp_path, world)
    with pytest.raises(CommandFailed) as caught:
        await h.run("review", {"args": '"never closed'})
    assert str(caught.value) == (
        '/review: a " in the arguments is never closed (usage: /review <file> [focus])'
    )
    await h.settle()
    assert (await h.view()).hook_error is None, "it is the caller's error, not a hook's"
    assert users(h) == []
    await h.close()


@scenario
async def test_a_template_that_would_send_nothing_is_refused(tmp_path: Path, world: World) -> None:
    template(world, "blank", "---\ndescription: Says nothing\n---\n")
    h = await session(tmp_path, world)
    with pytest.raises(CommandFailed, match="/blank: nothing to send"):
        await h.run("blank")
    await h.settle()
    assert users(h) == []
    await h.close()


@scenario
async def test_standard_input_follows_the_expansion_after_a_blank_line(
    tmp_path: Path, world: World
) -> None:
    template(world, "review", "Review $1")
    h = await session(tmp_path, world)
    await h.run("review", {"args": "a.rs", "stdin": "the diff"})
    await h.settle()
    assert users(h) == ["Review a.rs\n\nthe diff"]
    parsed = await h.client.parse_line(h.cursor, "/review a.rs stdin=x")
    assert parsed.args == {"args": "a.rs stdin=x"}, "the line never fills it"
    await h.close()


@scenario
async def test_a_skill_attaches_nothing_and_tells_the_model_where_its_paths_start(
    tmp_path: Path, world: World
) -> None:
    """A skill's body is extension-written text: `attachments` never expands it (D24 section 5),
    wherever the file it names is. The model is told the skill's directory and reads it itself."""
    path = skill(world, "pdf", "---\ndescription: d\n---\nRead @ref.md and @nope.md, @alice")
    (path.parent / "ref.md").write_text("REF", encoding="utf-8")
    work = tmp_path / "elsewhere"
    work.mkdir()
    (work / "ref.md").write_text("WORK", encoding="utf-8")
    h = await session(tmp_path, world, Attachments(work))
    await h.run("pdf")
    await h.settle()
    assert users(h) == [
        f'<skill name="pdf" path="{path}">\nRead @ref.md and @nope.md, @alice\n</skill>\n'
        f"A relative path in a skill starts from the directory of its file: {path.parent}."
    ]
    await h.close()


@scenario
async def test_a_byte_order_mark_is_not_part_of_the_file(tmp_path: Path, world: World) -> None:
    (world.tau / "prompts").mkdir(parents=True)
    (world.tau / "prompts" / "bom.md").write_bytes(
        "\ufeff---\ndescription: Has a mark\n---\nBody".encode()
    )
    h = await session(tmp_path, world)
    form = next(f for f in await h.client.commands() if f.qualified == "skills:bom")
    assert form.description == "Has a mark"
    await h.run("bom")
    await h.settle()
    assert users(h) == ["Body"]
    await h.close()


# -- files that change while it runs


@scenario
async def test_edited_added_and_deleted_templates_are_noticed_before_a_command_runs(
    tmp_path: Path, world: World
) -> None:
    template(world, "old", "OLD $ARGUMENTS")
    template(world, "gone", "GONE")
    h = await session(tmp_path, world, replies=3)
    template(world, "old", "EDITED $ARGUMENTS")
    template(world, "fresh", "FRESH $ARGUMENTS")
    (world.tau / "prompts" / "gone.md").unlink()
    names = [f.name for f in await h.client.commands()]
    assert "fresh" in names
    assert "gone" not in names
    await h.run("old", {"args": "a"})
    await h.settle()
    await h.run("fresh", {"args": "b"})
    await h.settle()
    assert users(h) == ["EDITED a", "FRESH b"]
    with pytest.raises(LookupError):
        await h.run("gone")
    await h.close()


@scenario
async def test_a_new_name_is_known_to_the_line_before_it_is_called_unknown(
    tmp_path: Path, world: World
) -> None:
    h = await session(tmp_path, world)
    template(world, "late", "LATE")
    parsed = await h.client.parse_line(h.cursor, "/late")
    assert parsed.command.qualified == "skills:late"
    assert "/late" in await h.client.complete_line(h.cursor, "/la")
    await h.close()


@scenario
async def test_the_skills_list_the_model_sees_is_still_written_once(
    tmp_path: Path, world: World
) -> None:
    skill(world, "pdf", "---\ndescription: d\n---\nb")
    h = await session(tmp_path, world, tools=True)
    skill(world, "later", "---\ndescription: added after the start\n---\nb")
    await h.send("hello")
    await h.settle()
    assert "later" not in prefix(h)[1], "the list is the prefix: it was read at the start"
    assert "skills:later" in [f.qualified for f in await h.client.commands()]
    await h.close()


@scenario
async def test_a_file_edited_into_an_error_is_a_command_that_says_so(
    tmp_path: Path, world: World
) -> None:
    template(world, "fine", "ok")
    template(world, "bad", "ok")
    h = await session(tmp_path, world)
    template(world, "bad", "---\nnot frontmatter\n---\nx")
    with pytest.raises(CommandFailed, match="expected `key: value`"):
        await h.run("bad")
    await h.settle()
    await h.run("fine")  # the others still work
    await h.settle()
    assert users(h) == ["ok"]
    await h.close()


@scenario
async def test_a_project_file_changed_since_it_was_trusted_is_not_used(
    tmp_path: Path, world: World
) -> None:
    root = world.project()
    path = root / ".tau" / "prompts" / "deploy.md"
    path.parent.mkdir()
    path.write_text("approved text", encoding="utf-8")
    trust(world, root)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([DONE] * 2),
        extensions=[Skills.of(loaded(world, root))],
    )
    path.write_text("swapped text", encoding="utf-8")
    with pytest.raises(CommandFailed, match="has changed since you approved it") as caught:
        await h.run("deploy")
    assert "tau trust" in str(caught.value)
    await h.settle()
    assert users(h) == []
    assert tau(world, "trust", cwd=root).code == 0
    await h.run("deploy")  # trusted again: the new text
    await h.settle()
    assert users(h) == ["swapped text"]
    await h.close()
