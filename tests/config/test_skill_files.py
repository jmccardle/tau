"""Prompt templates and skills (D24 section 6) as files: where they are found, which win, the
project's pinned by trust like `.tau/SYSTEM.md`, and every file that stops the start. Through
`tau.load_extensions`, `tau.list_extensions` and `tau.cli.main` on real files; what the commands
do is in `tests/behavior/test_skills.py`."""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from world import World, loaded, tau, trust

from tau import ExtensionLoadError, Skills, UntrustedProject, load_config, load_extensions

SKILL = "---\ndescription: Does a thing\n---\nBody of the skill\n"


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    before = os.getcwd()
    yield
    os.chdir(before)


def put(base: Path, relative: str, text: str) -> Path:
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # not write_text: Windows would turn \n into \r\n
    return path


def commands(world: World, cwd: Path | None = None) -> list[str]:
    """The commands the `skills` row of `tau ext list` has."""
    result = tau(world, "ext", "list", "--json", cwd=cwd)
    rows = {r["key"]: r for r in map(json.loads, result.out.splitlines())}
    return list(rows["skills"]["commands"])


def skills(world: World, cwd: Path | None = None) -> Skills:
    (found,) = [e for e in load_extensions(loaded(world, cwd)) if isinstance(e, Skills)]
    return found


def failure(world: World, cwd: Path | None = None) -> str:
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(loaded(world, cwd))
    return str(caught.value)


def warned(world: World, cwd: Path | None = None) -> str:
    """What `skills` says about the files it skipped: the start loads, and says these."""
    return "\n".join(skills(world, cwd).warnings)


# -- where they are


def test_the_users_templates_and_skills_are_commands_by_name(world: World) -> None:
    put(world.tau, "prompts/review.md", "Review $1")
    put(world.tau, "prompts/another.md", "x")
    put(world.tau, "skills/pdf/SKILL.md", SKILL)
    assert commands(world) == ["skills:another", "skills:pdf", "skills:review"]


def test_other_files_and_hidden_ones_are_not_prompts(world: World) -> None:
    put(world.tau, "prompts/notes.txt", "not a template")
    put(world.tau, "prompts/.hidden.md", "hidden")
    put(world.tau, "skills/empty/README.md", "a directory with no SKILL.md")
    put(world.tau, "skills/.git/SKILL.md", "hidden")
    put(world.tau, "skills/real/SKILL.md", SKILL)
    assert commands(world) == ["skills:real"]


def test_no_directories_is_no_commands_and_no_error(world: World) -> None:
    assert commands(world) == []


def test_load_false_turns_it_off(world: World) -> None:
    put(world.tau, "prompts/review.md", "Review $1")
    assert tau(world, "ext", "disable", "skills").code == 0
    assert commands(world) == []
    assert "skills" not in [e.name for e in load_extensions(loaded(world))]


def test_no_extensions_does_not_turn_it_off(world: World) -> None:
    put(world.tau, "prompts/review.md", "Review $1")
    result = tau(world, "ext", "list", "--json", "--no-extensions")
    rows = {r["key"]: r for r in map(json.loads, result.out.splitlines())}
    assert rows["skills"]["state"] == "on"
    assert rows["skills"]["commands"] == ["skills:review"]


# -- the project's, pinned by trust


def test_a_project_that_is_not_trusted_has_no_templates(world: World) -> None:
    root = world.project()
    put(root, ".tau/prompts/deploy.md", "Deploy $1")
    assert commands(world, root) == []


def test_a_trusted_projects_files_are_commands_too(world: World) -> None:
    put(world.tau, "prompts/review.md", "Review $1")
    root = world.project()
    put(root, ".tau/prompts/deploy.md", "Deploy $1")
    put(root, ".tau/skills/lint/SKILL.md", SKILL)
    trust(world, root)
    assert commands(world, root) == ["skills:deploy", "skills:lint", "skills:review"]


def test_for_one_name_the_project_wins_over_the_user(world: World) -> None:
    put(world.tau, "prompts/review.md", "user's")
    put(world.tau, "skills/lint/SKILL.md", SKILL)
    root = world.project()
    mine = put(root, ".tau/prompts/review.md", "project's")
    other = put(root, ".tau/skills/lint/SKILL.md", "---\ndescription: p\n---\nproject lint")
    trust(world, root)
    found = {e.name: e for e in skills(world, root).prompts}
    assert (found["review"].body, found["review"].path) == ("project's", mine)
    assert (found["lint"].body, found["lint"].path) == ("project lint", other)
    assert len(found) == 2


def test_the_project_wins_across_kinds(world: World) -> None:
    put(world.tau, "prompts/review.md", "user's template")
    root = world.project()
    put(root, ".tau/skills/review/SKILL.md", "---\ndescription: p\n---\nproject skill")
    trust(world, root)
    (prompt,) = skills(world, root).prompts
    assert (prompt.kind, prompt.body) == ("skill", "project skill")


def test_the_trust_report_lists_the_files_it_pins(world: World) -> None:
    root = world.project()
    put(root, ".tau/prompts/deploy.md", "Deploy $1")
    put(root, ".tau/skills/lint/SKILL.md", SKILL)
    report = tau(world, "trust", cwd=root).out
    assert "Its prompt templates and skills" in report
    assert ".tau/prompts/deploy.md" not in report, "listed by path under .tau, as the others are"
    assert "prompts/deploy.md  9 bytes" in report
    assert f"skills/lint/SKILL.md  {len(SKILL.encode())} bytes" in report


def test_a_prompt_changed_after_approval_untrusts_the_project(world: World) -> None:
    root = world.project()
    path = put(root, ".tau/prompts/deploy.md", "Deploy $1")
    trust(world, root)
    path.write_text("Deploy $1, then delete everything", encoding="utf-8")
    with pytest.raises(UntrustedProject) as caught:
        load_config(root, world.environ, ask=None)
    assert ".tau/prompts/deploy.md changed" in str(caught.value)
    assert commands(world, root) == []


def test_a_prompt_added_or_removed_after_approval_untrusts_the_project(world: World) -> None:
    root = world.project()
    first = put(root, ".tau/prompts/one.md", "1")
    trust(world, root)
    put(root, ".tau/skills/new/SKILL.md", SKILL)
    with pytest.raises(UntrustedProject, match=r"\.tau/skills/new/SKILL\.md added"):
        load_config(root, world.environ, ask=None)
    tau(world, "trust", cwd=root)
    first.unlink()
    with pytest.raises(UntrustedProject, match=r"\.tau/prompts/one\.md removed"):
        load_config(root, world.environ, ask=None)


def test_the_approved_bytes_are_what_is_read_at_the_start(world: World) -> None:
    root = world.project()
    path = put(root, ".tau/prompts/deploy.md", "approved text")
    trust(world, root)
    config = loaded(world, root)
    path.write_text("swapped text", encoding="utf-8")
    (found,) = [e for e in load_extensions(config) if isinstance(e, Skills)]
    assert found.prompts[0].body == "approved text"


def test_a_project_file_changed_after_approval_is_read_again_once_not_on_every_listing(
    world: World,
) -> None:
    """The stale file is a command that says to run `tau trust`; until a stat says something
    changed again, `rescan` has nothing to do (it does not hash every project file each time)."""
    root = world.project()
    path = put(root, ".tau/prompts/deploy.md", "approved text")
    trust(world, root)
    (found,) = [e for e in load_extensions(loaded(world, root)) if isinstance(e, Skills)]
    assert found.rescan() is False
    path.write_text("swapped text, longer", encoding="utf-8")
    assert found.rescan() is True, "the edit is noticed"
    assert found.rescan() is False, "and not again while the files are as they were"
    path.write_text("swapped again, longer still", encoding="utf-8")
    assert found.rescan() is True


def forget_prompts(world: World, root: Path) -> None:
    """Make the record what a τ from before prompts were pinned wrote."""
    state = world.tau / "trusted.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    del data["projects"][str(root.resolve())]["prompts_sha256"]
    del data["projects"][str(root.resolve())]["prompts"]
    state.write_text(json.dumps(data), encoding="utf-8")


def test_a_project_trusted_before_prompts_were_pinned_stays_trusted_if_it_has_none(
    world: World,
) -> None:
    root = world.project()
    trust(world, root)
    forget_prompts(world, root)
    assert "already trusted" in tau(world, "trust", cwd=root).out
    assert load_config(root, world.environ, ask=None).project is not None


def test_one_that_has_prompt_files_asks_again_and_says_which_were_added(world: World) -> None:
    root = world.project()
    trust(world, root)
    forget_prompts(world, root)
    put(root, ".tau/prompts/deploy.md", "Deploy")
    report = tau(world, "trust", cwd=root).out
    assert "has changed since you approved it" in report
    assert ".tau/prompts/deploy.md added" in report


def test_a_symlinked_prompt_is_read_and_pinned_by_its_bytes(world: World, tmp_path: Path) -> None:
    root = world.project()
    outside = put(tmp_path, "elsewhere.md", "secret")
    (root / ".tau" / "prompts").mkdir()
    (root / ".tau" / "prompts" / "link.md").symlink_to(outside)
    report = tau(world, "trust", cwd=root).out
    assert f"a symlink: these are the bytes of {outside.resolve()}" in report
    assert [p.body for p in skills(world, root).prompts] == ["secret"]
    outside.write_text("changed", encoding="utf-8")
    with pytest.raises(UntrustedProject, match=r"\.tau/prompts/link\.md changed"):
        load_config(root, world.environ, ask=None)
    again = tau(world, "trust", cwd=root).out
    assert "changed" in again
    assert [p.body for p in skills(world, root).prompts] == ["changed"]


def test_a_symlinked_skill_directory_is_read_and_its_target_is_in_the_report(
    world: World, tmp_path: Path
) -> None:
    root = world.project()
    put(tmp_path, "elsewhere/SKILL.md", SKILL)
    (root / ".tau" / "skills").mkdir()
    (root / ".tau" / "skills" / "linked").symlink_to(tmp_path / "elsewhere")
    report = tau(world, "trust", cwd=root).out
    assert (
        f"a symlink: these are the bytes of {(tmp_path / 'elsewhere' / 'SKILL.md').resolve()}"
        in (report)
    )
    assert [p.name for p in skills(world, root).prompts] == ["linked"]


def test_the_skills_directory_may_link_to_one_the_project_keeps_elsewhere(world: World) -> None:
    root = world.project()
    put(root, "team/skills/pdf/SKILL.md", SKILL)
    (root / ".tau" / "skills").symlink_to(root / "team" / "skills")
    report = tau(world, "trust", cwd=root).out
    # the listing names a file relative to `.tau/`, as it is read through the link
    assert f"  skills/pdf/SKILL.md  {len(SKILL.encode())} bytes" in report.splitlines()
    target = (root / "team" / "skills" / "pdf" / "SKILL.md").resolve()
    assert f"a symlink: these are the bytes of {target}" in report
    assert [p.name for p in skills(world, root).prompts] == ["pdf"]


def test_a_symlinked_skill_file_and_a_symlinked_prompts_directory_are_read_too(
    world: World, tmp_path: Path
) -> None:
    root = world.project()
    put(tmp_path, "shared/prompts/one.md", "Review $1")
    (root / ".tau" / "prompts").symlink_to(tmp_path / "shared" / "prompts")
    put(tmp_path, "file/SKILL.md", SKILL)
    (root / ".tau" / "skills" / "two").mkdir(parents=True)
    (root / ".tau" / "skills" / "two" / "SKILL.md").symlink_to(tmp_path / "file" / "SKILL.md")
    trust(world, root)
    assert [p.name for p in skills(world, root).prompts] == ["one", "two"]


def test_a_symlink_that_leads_nowhere_is_still_refused(world: World, tmp_path: Path) -> None:
    root = world.project()
    (root / ".tau" / "prompts").mkdir()
    (root / ".tau" / "prompts" / "gone.md").symlink_to(tmp_path / "missing.md")
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "gone.md is a symlink to nothing" in result.err


def test_a_symlink_to_a_fifo_or_a_directory_is_refused_as_it_is_without_one(
    world: World, tmp_path: Path
) -> None:
    root = world.project()
    (root / ".tau" / "prompts").mkdir()
    os.mkfifo(tmp_path / "pipe.md")
    (root / ".tau" / "prompts" / "pipe.md").symlink_to(tmp_path / "pipe.md")
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "pipe.md is not a regular file" in result.err


def test_a_symlink_to_an_oversize_file_is_refused(world: World, tmp_path: Path) -> None:
    root = world.project()
    (root / ".tau" / "prompts").mkdir()
    big = put(tmp_path, "big.md", "x" * ((1 << 20) + 1))
    (root / ".tau" / "prompts" / "big.md").symlink_to(big)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "big.md is 1048577 bytes; the limit is 1048576" in result.err


def test_a_symlinked_tau_directory_is_still_not_a_project(world: World, tmp_path: Path) -> None:
    root = world.home / "linked"
    root.mkdir()
    put(tmp_path, "real/prompts/x.md", "x")
    (root / ".tau").symlink_to(tmp_path / "real")
    result = tau(world, "trust", cwd=root)
    assert result.code != 0
    assert "symlink" in result.err


def test_trusted_json_records_the_prompt_hashes(world: World) -> None:
    root = world.project()
    put(root, ".tau/prompts/deploy.md", "Deploy")
    trust(world, root)
    record = json.loads((world.tau / "trusted.json").read_text(encoding="utf-8"))["projects"][
        str(root.resolve())
    ]
    assert list(record["prompts"]) == ["prompts/deploy.md"]
    assert len(record["prompts_sha256"]) == 64


# -- a file that stops the start


def test_a_template_and_a_skill_with_one_name_in_one_place_name_both(world: World) -> None:
    template = put(world.tau, "prompts/review.md", "t")
    skill = put(world.tau, "skills/review/SKILL.md", SKILL)
    message = failure(world)
    assert 'extension "skills" failed to load; tau did not start.' in message
    assert f"/review is both a template ({template}) and a skill ({skill}): rename one" in message
    assert "to start without it: tau ext disable skills" in message
    assert "--no-extensions" not in message, "it does not turn a built-in off"


def test_the_same_name_in_the_users_and_the_projects_is_not_a_clash(world: World) -> None:
    put(world.tau, "prompts/review.md", "t")
    root = world.project()
    put(root, ".tau/skills/review/SKILL.md", SKILL)
    trust(world, root)
    assert commands(world, root) == ["skills:review"]


def test_a_name_that_is_not_letters_digits_underscore_and_hyphen_is_skipped_with_a_warning(
    world: World,
) -> None:
    path = put(world.tau, "prompts/my notes.md", "t")
    assert f"{path}: the name 'my notes' is not ASCII letters, digits, `_` and `-`" in warned(world)


def test_a_skill_needs_a_description(world: World) -> None:
    path = put(world.tau, "skills/pdf/SKILL.md", "no frontmatter at all")
    assert f"{path}: no description, and a skill needs a `description`" in warned(world)


def test_an_empty_template_with_no_description_is_skipped_with_a_warning(world: World) -> None:
    path = put(world.tau, "prompts/empty.md", "---\nargument-hint: x\n---\n\n")
    assert f"{path}: no description, and it has no text" in warned(world)


def test_a_name_in_the_frontmatter_must_be_the_files(world: World) -> None:
    path = put(world.tau, "prompts/review.md", "---\nname: other\n---\nt")
    assert f"{path}: `name: other` is not the file's name, 'review'" in warned(world)


def test_a_frontmatter_that_never_closes_is_skipped_with_a_warning(world: World) -> None:
    path = put(world.tau, "prompts/review.md", "---\ndescription: d\nbody")
    assert f"{path}: the frontmatter opens with --- and never closes" in warned(world)


def test_a_frontmatter_line_without_a_colon_is_skipped_with_a_warning(world: World) -> None:
    path = put(world.tau, "prompts/review.md", "---\ndescription d\n---\nbody")
    assert f"{path}:2: expected `key: value`, got 'description d'" in warned(world)


def test_a_description_that_is_a_list_is_skipped_with_a_warning(world: World) -> None:
    path = put(world.tau, "prompts/review.md", "---\ndescription:\n  - one\n---\nbody")
    assert f"{path}: `description` must be text, not empty, a list or a mapping" in warned(world)


def test_an_empty_block_scalar_description_is_skipped_with_a_warning(world: World) -> None:
    path = put(world.tau, "skills/pdf/SKILL.md", "---\ndescription: >\nname: pdf\n---\nbody")
    assert f"{path}: `description` must be text, not empty, a list or a mapping" in warned(world)


def test_a_quote_that_never_closes_is_skipped_with_a_warning(world: World) -> None:
    path = put(world.tau, "skills/pdf/SKILL.md", '---\nname: pdf\ndescription: "open\n---\nb')
    assert f'{path}:3: the " that opens `description` never closes' in warned(world)


def test_an_unknown_escape_in_a_double_quoted_value_is_skipped_with_a_warning(world: World) -> None:
    path = put(world.tau, "skills/pdf/SKILL.md", '---\ndescription: "a \\q b"\n---\nb')
    assert f"{path}:2: `description` has an unknown escape, \\q" in warned(world)


def test_disable_model_invocation_must_be_true_or_false(world: World) -> None:
    path = put(
        world.tau,
        "skills/pdf/SKILL.md",
        "---\ndescription: d\ndisable-model-invocation: maybe\n---\nb",
    )
    assert f"{path}: `disable-model-invocation` is 'maybe', not true or false" in warned(world)


def test_a_file_that_is_not_utf8_is_skipped_with_a_warning(world: World) -> None:
    path = world.tau / "prompts" / "bad.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")
    assert f"{path}: not valid UTF-8" in warned(world)


def test_a_template_named_config_is_skipped_with_a_warning(world: World) -> None:
    put(world.tau, "prompts/config.md", "t")
    assert "the name 'config' is the built-in /config's" in warned(world)
    assert skills(world).prompts == ()


# -- what a frontmatter may hold


def test_quotes_comments_indented_lines_and_other_keys_are_read_as_a_claude_skill_has_them(
    world: World,
) -> None:
    put(
        world.tau,
        "skills/pdf/SKILL.md",
        '---\n# a comment\nname: pdf\ndescription: "Quoted: with a colon"\n'
        "allowed-tools: [Read]\nmetadata:\n  version: 1\nargument-hint: '<file>'\n---\n\nBody\n\n",
    )
    (prompt,) = skills(world).prompts
    assert (prompt.description, prompt.hint, prompt.body) == (
        "Quoted: with a colon",
        "<file>",
        "Body",
    )
    assert prompt.listed


CLAUDE_SKILL = """\
---
name: pdf-processing
# Claude Code reads this description to decide when to use the skill.
description: >-
  Extract text and tables from PDF files, fill forms, merge documents.
  Use when working with PDF files or when the user mentions PDFs,
  forms, or document extraction.
allowed-tools:
- Read
- Bash(python:*)
metadata:
  version: "1.2"
  tags: [pdf, documents]
license: Apache-2.0
---

# PDF processing

Read `reference.md` first.
"""


def test_a_claude_code_skill_with_a_folded_description_loads_as_it_is(world: World) -> None:
    """The regression for 0.17.3: a folded `description: >-` stopped tau from starting."""
    put(world.tau, "skills/pdf-processing/SKILL.md", CLAUDE_SKILL)
    (prompt,) = skills(world).prompts
    assert prompt.description == (
        "Extract text and tables from PDF files, fill forms, merge documents. Use when working "
        "with PDF files or when the user mentions PDFs, forms, or document extraction."
    )
    assert prompt.body == "# PDF processing\n\nRead `reference.md` first."
    assert prompt.listed
    assert commands(world) == ["skills:pdf-processing"]


BLOCKS = [
    ("description: |\n  line one\n  line two\n", "line one\nline two"),
    ("description: |+\n  kept\n\n", "kept"),
    (
        "description: >\n  folded\n  over lines\n\n  new paragraph\n",
        "folded over lines\nnew paragraph",
    ),
    ("description: >\n  text\n    indented more\n  back\n", "text\n  indented more\nback"),
    ("description: >2  # a comment\n   leading space\n", "leading space"),
    ("description: Plain text\n  that goes on # and a comment\n", "Plain text that goes on"),
    ("description:\n  on the next line\n", "on the next line"),
    ("description: Use C# here # not this\n", "Use C# here"),
    (
        'description: "double \\"quoted\\" \\u00e9\\tand\n  folded"\n',
        'double "quoted" \u00e9\tand folded',
    ),
    ('description: "escaped \\\n  break"\n', "escaped break"),
    ("description: 'it''s single'\n", "it's single"),
    ("description: 'single\n\n  with a blank line'\n", "single\nwith a blank line"),
    ("description: [flow, text]\n", "[flow, text]"),
]


@pytest.mark.parametrize(("frontmatter", "description"), BLOCKS)
def test_yaml_values_are_read_as_yaml_reads_them(
    world: World, frontmatter: str, description: str
) -> None:
    put(world.tau, "skills/pdf/SKILL.md", f"---\n{frontmatter}---\nBody\n")
    (prompt,) = skills(world).prompts
    assert prompt.description == description


def test_other_keys_are_not_read_so_even_broken_yaml_in_one_does_not_stop_the_start(
    world: World,
) -> None:
    put(
        world.tau,
        "skills/pdf/SKILL.md",
        '---\ndescription: d\nmetadata: "never closed\nother: "\\q"\nlist:\n- a\n---\nb',
    )
    (prompt,) = skills(world).prompts
    assert prompt.description == "d"


def test_disable_model_invocation_reads_the_yaml_spellings_of_true(world: World) -> None:
    put(
        world.tau,
        "skills/pdf/SKILL.md",
        "---\ndescription: d\ndisable-model-invocation: True\n---\nb",
    )
    (prompt,) = skills(world).prompts
    assert not prompt.listed


def test_crlf_files_are_read(world: World) -> None:
    put(world.tau, "prompts/crlf.md", "---\r\ndescription: d\r\n---\r\nline one\r\nline two\r\n")
    (prompt,) = skills(world).prompts
    assert (prompt.description, prompt.body) == ("d", "line one\nline two")


# -- files in the wrong place, and every problem at once

MISPLACED = [
    ("skills/pdf/skill.md", "pdf", "misplaced skill: rename it SKILL.md"),
    ("skills/pdf.md", "pdf", "misplaced skill: a skill is a directory"),
    ("skills/group/pdf/SKILL.md", "pdf", "misplaced skill: move it to skills/pdf/SKILL.md"),
    ("prompts/sub/x.md", "x", "misplaced template: move it to prompts/x.md"),
    ("prompts/x.MD", "x", "misplaced template: rename it x.md"),
]


@pytest.mark.parametrize(("relative", "name", "message"), MISPLACED)
def test_a_file_in_the_wrong_place_is_skipped_with_a_warning(
    world: World, relative: str, name: str, message: str
) -> None:
    path = put(world.tau, relative, SKILL)
    assert f"{path}: {message}" in warned(world)


def test_every_problem_is_listed_at_once_and_the_good_files_still_load(world: World) -> None:
    put(world.tau, "prompts/a.b.md", "x")
    put(world.tau, "skills/bad/SKILL.md", "no frontmatter")
    put(world.tau, "prompts/z.MD", "x")
    put(world.tau, "prompts/fine.md", "x")
    message = warned(world)
    assert "a.b.md: the name 'a.b'" in message
    assert f"{Path('skills', 'bad', 'SKILL.md')}: no description" in message
    assert "z.MD: misplaced template" in message
    assert [p.name for p in skills(world).prompts] == ["fine"]


def test_the_start_prints_a_warning_naming_the_file_and_goes_on(world: World) -> None:
    path = put(world.tau, "skills/pdf/SKILL.md", "no frontmatter at all")
    put(world.tau, "prompts/fine.md", "ok")
    result = tau(world, "config", "check")
    assert result.code == 0, result.err
    assert f"warning: {path}: no description, and a skill needs a `description` (skipped)" in (
        result.out
    )
    assert commands(world) == ["skills:fine"]


def test_a_template_and_a_skill_with_one_name_still_stop_the_start_when_others_are_skipped(
    world: World,
) -> None:
    put(world.tau, "prompts/bad name.md", "x")
    put(world.tau, "prompts/review.md", "t")
    put(world.tau, "skills/review/SKILL.md", SKILL)
    message = failure(world)
    assert "/review is both a template" in message
    assert "bad name" not in message, "the skipped file is a warning, not part of the failure"


def test_an_unreadable_user_file_is_skipped_with_a_warning(world: World) -> None:
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("a file mode does not keep this reader out")
    path = put(world.tau, "prompts/locked.md", "x")
    path.chmod(0)
    try:
        assert f"{path}: cannot be read" in warned(world)
    finally:
        path.chmod(0o600)


def test_a_file_that_is_broken_in_the_project_is_skipped_and_does_not_stop_trust(
    world: World,
) -> None:
    root = world.project()
    put(root, ".tau/prompts/a.b.md", "x")
    put(root, ".tau/skills/bad/SKILL.md", "no frontmatter")
    put(root, ".tau/skills/loose.md", SKILL)
    put(root, ".tau/prompts/fine.md", "ok")
    result = tau(world, "trust", cwd=root)
    assert result.code == 0, result.err
    for line in ("a.b.md: the name 'a.b'", "bad/SKILL.md: no description", "loose.md: misplaced"):
        assert line in result.out, result.out
    assert "tau cannot load (skipped; it starts without them)" in result.out
    assert "cannot be trusted" not in result.out
    assert [p.name for p in skills(world, root).prompts] == ["fine"]
    assert (world.tau / "trusted.json").exists()


def test_tau_trust_still_refuses_a_name_two_files_claim(world: World) -> None:
    root = world.project()
    put(root, ".tau/prompts/review.md", "t")
    put(root, ".tau/skills/review/SKILL.md", SKILL)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "/review is both a template" in result.err
    assert "cannot be trusted until these are fixed" in result.err
    assert not (world.tau / "trusted.json").exists()


def test_a_misplaced_file_added_after_approval_is_an_error_at_the_start(
    world: World,
) -> None:
    root = world.project()
    trust(world, root)
    put(root, ".tau/prompts/x.MD", "x")
    with pytest.raises(UntrustedProject, match=r"x\.MD: misplaced template"):
        load_config(root, world.environ, ask=None)


# -- the trust prompt shows what the model will read


def test_the_trust_report_shows_the_text_of_new_prompt_files_and_cuts_a_long_one(
    world: World,
) -> None:
    root = world.project()
    put(root, ".tau/prompts/deploy.md", "Deploy it\nthen leave\n")
    put(root, ".tau/prompts/long.md", "x" * 2500)
    report = tau(world, "trust", cwd=root).out
    assert "    Deploy it\n    then leave" in report
    assert "x" * 2000 in report
    assert "x" * 2001 not in report
    assert "cut: 500 more characters not shown" in report


def test_only_a_changed_file_shows_its_text_again(world: World) -> None:
    root = world.project()
    put(root, ".tau/prompts/same.md", "SAMETEXT")
    changed = put(root, ".tau/prompts/edit.md", "BEFORE")
    trust(world, root)
    changed.write_text("AFTERTEXT", encoding="utf-8")
    report = tau(world, "trust", cwd=root).out
    assert "AFTERTEXT" in report
    assert "SAMETEXT" not in report


# -- a byte order mark


def test_a_byte_order_mark_does_not_hide_the_frontmatter(world: World) -> None:
    path = world.tau / "skills" / "pdf" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(("﻿" + SKILL).encode())
    (found,) = skills(world).prompts
    assert (found.description, found.body) == ("Does a thing", "Body of the skill")
