"""Prompt templates and skills: discovery, precedence, expansion, and the one door.

Reference: docs/PROMPT-TEMPLATES-AND-SKILLS.md.

Pins, in order: the frontmatter subset (§2.3); the three scopes and first-wins
(§2.2) including the closed project scope for skills (§3); template argument
substitution and its two refusals (§4.2); the skill listing and ``/skill:``
expansion (§5); and that the expansion happens inside ``submit()`` behind
``expand_commands``, after built-ins and extension commands (§6).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.commands import (
    CommandCompletion,
    complete_command,
    expand_prompt_command,
    prompt_command_completions,
    resolve_command,
)
from tau_agent_core.projections import command_vocabulary
from tau_agent_core.prompt_templates import (
    PromptTemplate,
    expand_prompt_template,
    load_prompt_templates,
)
from tau_agent_core.resources import (
    BrokenResource,
    ResourceError,
    ResourceSource,
    parse_frontmatter,
    resource_locations,
)
from tau_agent_core.sdk import _build_system_prompt, _resolve_tools
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.skills import (
    Skill,
    expand_skill,
    load_skills,
    render_skills_block,
)
from tau_agent_core.submission import Submission

_TS = 1_700_000_000_000


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _template(body: str, name: str = "t") -> PromptTemplate:
    return PromptTemplate(
        name=name,
        description="d",
        argument_hint=None,
        body=body,
        source=ResourceSource(path=Path(f"/x/{name}.md"), scope="user"),
    )


# -- §2.3 frontmatter ---------------------------------------------------------


class TestFrontmatter:
    def test_no_frontmatter_is_all_body(self):
        parsed = parse_frontmatter("hello\n---\nworld")
        assert parsed.fields == {} and parsed.body == "hello\n---\nworld"

    def test_flat_and_quoted_values(self):
        parsed = parse_frontmatter(
            "---\nname: pdf\ndescription: 'it''s fine'\nargument-hint: \"<url>\"\n---\nBody"
        )
        assert parsed.fields == {
            "name": "pdf",
            "description": "it's fine",
            "argument-hint": "<url>",
        }
        assert parsed.body == "Body"

    def test_folded_block_joins_lines(self):
        parsed = parse_frontmatter(
            "---\ndescription: >\n  Use this skill when\n  the user asks.\n---\nB"
        )
        assert parsed.text("description") == "Use this skill when the user asks."

    def test_literal_block_keeps_lines(self):
        parsed = parse_frontmatter("---\nnotes: |\n  one\n  two\n---\n")
        assert parsed.text("notes") == "one\ntwo\n"

    def test_plain_value_continued_on_indented_lines(self):
        parsed = parse_frontmatter("---\ndescription: first\n  second\n---\n")
        assert parsed.text("description") == "first second"

    def test_nested_block_is_recorded_not_parsed_and_refuses_to_read_as_text(self):
        parsed = parse_frontmatter("---\nmetadata:\n  author: me\nname: x\n---\n")
        assert parsed.nested == frozenset({"metadata"})
        assert parsed.text("name") == "x"
        with pytest.raises(ResourceError, match="nested"):
            parsed.text("metadata")

    def test_unclosed_block_raises(self):
        with pytest.raises(ResourceError, match="never closed"):
            parse_frontmatter("---\nname: x\nbody")

    def test_unreadable_line_raises(self):
        with pytest.raises(ResourceError, match="expected 'key: value'"):
            parse_frontmatter("---\n- a list item\n---\n")

    def test_flow_syntax_raises_rather_than_reading_brackets_as_text(self):
        with pytest.raises(ResourceError, match="YAML syntax"):
            parse_frontmatter("---\nallowed-tools: [Read, Bash]\n---\n")


# -- §2.2 discovery and precedence --------------------------------------------


class TestTemplateDiscovery:
    def test_explicit_beats_user_beats_project_and_the_losers_are_kept(self, tmp_path):
        home, cwd, extra = tmp_path / "home", tmp_path / "proj", tmp_path / "extra"
        _write(home / "prompts" / "review.md", "user review")
        _write(cwd / ".tau" / "prompts" / "review.md", "project review")
        _write(cwd / ".tau" / "prompts" / "only.md", "project only")
        _write(extra / "review.md", "explicit review")
        locations = resource_locations("prompts", cwd=cwd, agent_dir=home, paths=[extra])

        catalog = load_prompt_templates(locations)

        review = catalog.get("review")
        assert isinstance(review, PromptTemplate)
        assert review.body == "explicit review" and review.source.scope == "explicit"
        assert [e.source.scope for e in catalog.shadowed] == ["user", "project"]
        only = catalog.get("only")
        assert isinstance(only, PromptTemplate) and only.source.scope == "project"

    def test_discover_false_still_loads_explicit_paths(self, tmp_path):
        _write(tmp_path / "home" / "prompts" / "a.md", "a")
        one = _write(tmp_path / "one.md", "one")
        locations = resource_locations(
            "prompts", cwd=tmp_path, agent_dir=tmp_path / "home", paths=[one], discover=False
        )
        assert [e.name for e in load_prompt_templates(locations).entries] == ["one"]

    def test_a_missing_explicit_path_raises_and_a_missing_user_dir_does_not(self, tmp_path):
        locations = resource_locations(
            "prompts", cwd=tmp_path, agent_dir=tmp_path / "nohome", paths=["gone.md"]
        )
        with pytest.raises(ResourceError, match="gone.md"):
            load_prompt_templates(locations)
        empty = resource_locations("prompts", cwd=tmp_path, agent_dir=tmp_path / "nohome")
        assert load_prompt_templates(empty).entries == ()

    def test_a_bad_file_keeps_its_name_and_costs_nothing_else(self, tmp_path):
        home = tmp_path / "home"
        _write(home / "prompts" / "bad.md", "---\nname: x\n")
        _write(home / "prompts" / "bad name.md", "fine body")
        _write(home / "prompts" / "good.md", "---\ndescription: Good\n---\nbody")
        catalog = load_prompt_templates(resource_locations("prompts", cwd=tmp_path, agent_dir=home))
        bad = catalog.get("bad")
        assert isinstance(bad, BrokenResource) and "never closed" in bad.error
        assert isinstance(catalog.get("bad name"), BrokenResource)
        good = catalog.get("good")
        assert isinstance(good, PromptTemplate) and good.description == "Good"

    def test_description_falls_back_to_the_first_body_line(self, tmp_path):
        _write(tmp_path / "p" / "t.md", "\n\n" + "x" * 80 + "\nmore")
        catalog = load_prompt_templates(
            resource_locations("prompts", cwd=tmp_path, paths=[tmp_path / "p"], discover=False)
        )
        entry = catalog.get("t")
        assert isinstance(entry, PromptTemplate)
        assert entry.description == "x" * 60 + "..."


class TestSkillDiscovery:
    def test_project_skills_are_not_read_until_the_trust_gate_admits_them(self, tmp_path):
        _write(tmp_path / ".tau" / "skills" / "s" / "SKILL.md", "---\ndescription: d\n---\n")
        closed = resource_locations("skills", cwd=tmp_path, agent_dir=tmp_path / "home")
        assert closed.project is None
        assert load_skills(closed).entries == ()
        opened = resource_locations(
            "skills", cwd=tmp_path, agent_dir=tmp_path / "home", project=True
        )
        assert [e.name for e in load_skills(opened).entries] == ["s"]

    def test_project_templates_are_read(self, tmp_path):
        assert resource_locations("prompts", cwd=tmp_path).project is not None

    def test_a_directory_of_skills_and_a_single_skill_both_load(self, tmp_path):
        _write(tmp_path / "lib" / "alpha" / "SKILL.md", "---\ndescription: A\n---\nalpha body")
        _write(tmp_path / "lib" / "beta" / "SKILL.md", "---\nname: b\ndescription: B\n---\n")
        _write(tmp_path / "one" / "SKILL.md", "---\ndescription: One\n---\n")
        catalog = load_skills(
            resource_locations(
                "skills", cwd=tmp_path, paths=["lib", "one/SKILL.md"], discover=False
            )
        )
        assert [e.name for e in catalog.entries] == ["alpha", "b", "one"]
        alpha = catalog.get("alpha")
        assert isinstance(alpha, Skill) and alpha.body == "alpha body"

    def test_invalid_skills_are_broken_not_dropped(self, tmp_path):
        _write(tmp_path / "lib" / "nodesc" / "SKILL.md", "---\nname: nodesc\n---\n")
        _write(tmp_path / "lib" / "Bad_Name" / "SKILL.md", "---\ndescription: d\n---\n")
        catalog = load_skills(
            resource_locations("skills", cwd=tmp_path, paths=["lib"], discover=False)
        )
        broken = {e.name: e for e in catalog.entries}
        assert isinstance(broken["nodesc"], BrokenResource)
        assert "description" in broken["nodesc"].error
        assert isinstance(broken["Bad_Name"], BrokenResource)

    def test_disable_model_invocation_leaves_a_skill_out_of_the_listing(self, tmp_path):
        _write(
            tmp_path / "lib" / "hidden" / "SKILL.md",
            "---\ndescription: d\ndisable-model-invocation: true\n---\n",
        )
        _write(tmp_path / "lib" / "shown" / "SKILL.md", "---\ndescription: d\n---\n")
        catalog = load_skills(
            resource_locations("skills", cwd=tmp_path, paths=["lib"], discover=False)
        )
        assert [s.name for s in catalog.listed()] == ["shown"]
        assert isinstance(catalog.get("hidden"), Skill)


# -- §4.2 template expansion ---------------------------------------------------


class TestTemplateExpansion:
    def test_arguments_is_the_typed_text_verbatim(self):
        body = "Review: $ARGUMENTS"
        assert expand_prompt_template(_template(body), 'a  "b c"') == 'Review: a  "b c"'

    def test_positional_defaults_and_slices(self):
        body = "$1|${2:-none}|${@:2}|${@:1:1}|$@"
        assert expand_prompt_template(_template(body), 'x "y z" w') == 'x|y z|y z w|x|x "y z" w'
        assert expand_prompt_template(_template("${1:-dflt} ${ARGUMENTS:-all}"), "") == "dflt all"

    def test_a_missing_bare_positional_raises(self):
        with pytest.raises(ResourceError, match=r"uses \$2 but was given 1"):
            expand_prompt_template(_template("Make $1 with $2", "component"), "Button")

    def test_an_unbalanced_quote_raises_only_when_positions_are_read(self):
        with pytest.raises(ResourceError, match="cannot be split"):
            expand_prompt_template(_template("$1"), "don't")
        assert expand_prompt_template(_template("$ARGUMENTS"), "don't") == "don't"

    def test_no_placeholder_appends_the_arguments_rather_than_dropping_them(self):
        assert expand_prompt_template(_template("Body"), "extra") == "Body\n\nARGUMENTS: extra"
        assert expand_prompt_template(_template("Body"), "") == "Body"

    def test_a_placeholder_inside_the_arguments_is_not_substituted(self):
        assert expand_prompt_template(_template("say $1"), "$2") == "say $2"


# -- §5 skills in the prompt ---------------------------------------------------


def _skill(name: str = "pdf", description: str = "PDFs & <forms>") -> Skill:
    return Skill(
        name=name,
        description=description,
        body="Run ./x.sh",
        model_invocable=True,
        source=ResourceSource(path=Path(f"/skills/{name}/SKILL.md"), scope="user"),
    )


class TestSkillPrompt:
    def test_listing_escapes_and_names_the_file(self):
        block = render_skills_block([_skill()])
        assert "<description>PDFs &amp; &lt;forms&gt;</description>" in block
        assert "<location>/skills/pdf/SKILL.md</location>" in block
        assert render_skills_block([]) == ""

    def test_skill_command_wraps_the_body_and_appends_arguments(self):
        text = expand_skill(_skill(), "invoice.pdf")
        assert text.startswith('<skill name="pdf" location="/skills/pdf/SKILL.md">')
        assert "References are relative to /skills/pdf." in text
        assert text.endswith("</skill>\n\ninvoice.pdf")

    def test_system_prompt_lists_skills_only_when_the_model_can_read(self, tmp_path):
        read = _resolve_tools(["read"], None, str(tmp_path))
        with_read = _build_system_prompt(
            str(tmp_path), read, no_context_files=True, skills=[_skill()]
        )
        assert "<available_skills>" in with_read
        without = _build_system_prompt(str(tmp_path), [], no_context_files=True, skills=[_skill()])
        assert "<available_skills>" not in without

    def test_skills_field_moves_the_block(self, tmp_path):
        read = _resolve_tools(["read"], None, str(tmp_path))
        prompt = _build_system_prompt(
            str(tmp_path),
            read,
            custom_prompt="TOP\n{{skills}}\nBOTTOM",
            no_context_files=True,
            skills=[_skill()],
        )
        assert prompt.count("<available_skills>") == 1
        assert prompt.index("TOP") < prompt.index("<available_skills>") < prompt.index("BOTTOM")


# -- §6 the one door -----------------------------------------------------------


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta="ok", partial=_assistant("ok"))
            yield DoneEvent(final=_assistant("ok"), usage=_assistant("ok").usage)

        return _gen()

    async def result(self):
        return _assistant("ok")

    def abort(self):
        pass


def _catalogs(tmp_path: Path):
    _write(tmp_path / "p" / "review.md", "---\ndescription: Review it\n---\nReview $ARGUMENTS")
    _write(tmp_path / "p" / "compact.md", "never reached")
    _write(tmp_path / "p" / "ledger.md", "never reached either")
    _write(tmp_path / "s" / "pdf" / "SKILL.md", "---\ndescription: PDFs\n---\nUse pdftotext.")
    templates = load_prompt_templates(
        resource_locations("prompts", cwd=tmp_path, paths=["p"], discover=False)
    )
    skills = load_skills(resource_locations("skills", cwd=tmp_path, paths=["s"], discover=False))
    return templates, skills


def _sub(text: str, *, expand: bool = True) -> Submission:
    return Submission(
        text=text,
        source="interactive",
        submitter="human",
        submission_id="s-1",
        multitask_strategy="enqueue",
        expand_commands=expand,
    )


def _user_texts(session: AgentSession) -> list[str]:
    out = []
    for message in session.messages:
        if message.get("role") != "user":
            continue
        content = message["content"]
        out.append(content if isinstance(content, str) else "".join(b["text"] for b in content))
    return out


class TestOneDoor:
    async def _run(self, tmp_path: Path, sub: Submission) -> tuple[AgentSession, list]:
        templates, skills = _catalogs(tmp_path)
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            tools=[],
            prompt_templates=templates,
            skills=skills,
        )
        seen: list = []

        async def _stream_simple(model, context, options=None):
            seen.append(context)
            return _Stream()

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            result = await session.submit(sub)
        assert result.accepted and result.command is None
        return session, seen

    async def test_a_template_expands_and_the_expansion_is_what_persists(self, tmp_path):
        session, seen = await self._run(tmp_path, _sub("/review the parser"))
        assert _user_texts(session) == ["Review the parser"]
        assert "Review the parser" in str(seen[0]) and "/review" not in str(seen[0])

    async def test_a_skill_command_expands(self, tmp_path):
        session, _ = await self._run(tmp_path, _sub("/skill:pdf invoice.pdf"))
        (text,) = _user_texts(session)
        assert "Use pdftotext." in text and text.endswith("invoice.pdf")

    async def test_without_expand_commands_the_line_is_literal(self, tmp_path):
        session, _ = await self._run(tmp_path, _sub("/review the parser", expand=False))
        assert _user_texts(session) == ["/review the parser"]

    async def test_an_unknown_name_is_sent_as_typed(self, tmp_path):
        session, _ = await self._run(tmp_path, _sub("/nosuch thing"))
        assert _user_texts(session) == ["/nosuch thing"]

    async def test_a_broken_template_refuses_the_submission(self, tmp_path):
        _write(tmp_path / "p" / "broken.md", "---\nx: [1]\n---\n")
        templates, skills = _catalogs(tmp_path)
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            prompt_templates=templates,
            skills=skills,
        )
        with pytest.raises(ResourceError, match="/broken cannot run"):
            await session.submit(_sub("/broken"))
        assert _user_texts(session) == []

    def test_built_ins_and_extension_commands_resolve_before_a_template(self, tmp_path):
        templates, skills = _catalogs(tmp_path)
        assert resolve_command("/compact") is not None
        assert resolve_command("/ledger", ["ledger"]) is not None
        listed = prompt_command_completions(templates, skills, taken={"ledger"})
        assert [c.name for c in listed] == ["review", "skill:pdf"]

    def test_expand_prompt_command_routes_by_prefix(self, tmp_path):
        templates, skills = _catalogs(tmp_path)
        assert expand_prompt_command("/review x", templates, skills) == "Review x"
        assert expand_prompt_command("/skill:review x", templates, skills) is None
        assert expand_prompt_command("plain text", templates, skills) is None

    def test_completion_offers_templates_after_built_ins_and_extensions(self):
        prompt = (
            CommandCompletion("review", "Review it", "template"),
            CommandCompletion("speak", "shadowed", "template"),
        )
        found = complete_command("/", {"speak": "ext"}, prompt)
        assert found is not None
        origins = {m.name: m.origin for m in found.matches}
        assert origins["review"] == "template" and origins["speak"] == "extension"

    def test_the_command_listing_names_templates_and_skills(self, tmp_path):
        templates, skills = _catalogs(tmp_path)
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            prompt_templates=templates,
            skills=skills,
        )
        rows = {r["name"]: r for r in command_vocabulary(session)}
        assert rows["review"]["origin"] == "template" and rows["review"]["flow"] is False
        assert rows["skill:pdf"]["origin"] == "skill"
        assert rows["compact"]["origin"] == "builtin"
