"""The project's `.env` (D13): our own grammar, read only through a trusted project, and
errors that name file and line, never the value."""

from pathlib import Path

import pytest
from world import World, models, tau, trust

from tau_next import DotenvError, UntrustedProject, load_config

SECRET = "sk-secret-123"


def trusted_project(world: World) -> Path:
    root = world.project(config=models(p={}))
    trust(world, root)
    return root


def dotenv(world: World, text: str | bytes) -> dict[str, str]:
    """What `text` as a trusted project's `.env` adds to the shell environment."""
    root = trusted_project(world)
    path = root / ".env"
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    environ = load_config(root, world.environ, ask=None).environ
    return {k: v for k, v in environ.items() if k not in world.environ}


def test_plain_lines_comments_and_export(world: World) -> None:
    text = "# a comment\n\nA=1\nexport B=two\n  C = three  \nexport   D=4\nURL=http://h/?a=b\n"
    assert dotenv(world, text) == {
        "A": "1",
        "B": "two",
        "C": "three",
        "D": "4",
        "URL": "http://h/?a=b",
    }


def test_quotes(world: World) -> None:
    text = 'A=\'x\\ny # not a comment $HOME\'\nB="line\\nnext \\"q\\" back\\\\slash"\n'
    assert dotenv(world, text) == {
        "A": "x\\ny # not a comment $HOME",
        "B": 'line\nnext "q" back\\slash',
    }


def test_a_hash_starts_a_comment_only_at_the_line_start_or_after_whitespace(world: World) -> None:
    text = "A=value # note\nB=a#b\nC=\nD=   # only a note\nE='x' # why\nF=\"y\"\t# z\nG=#x\n"
    assert dotenv(world, text) == {
        "A": "value",
        "B": "a#b",
        "C": "",
        "D": "",
        "E": "x",
        "F": "y",
        "G": "#x",
    }


def test_a_byte_order_mark_is_tolerated_and_a_later_line_wins(world: World) -> None:
    assert dotenv(world, b"\xef\xbb\xbfA=1\nB=2\nA=3\n") == {"A": "3", "B": "2"}


def test_the_shell_overrides_dotenv(world: World) -> None:
    root = trusted_project(world)
    (root / ".env").write_text("KEY=from-file\nONLY=file\n", encoding="utf-8")
    environ = {**world.environ, "KEY": "from-shell"}
    merged = load_config(root, environ, ask=None).environ
    assert merged["KEY"] == "from-shell"
    assert merged["ONLY"] == "file"


def test_an_untrusted_projects_dotenv_is_not_read(world: World) -> None:
    root = world.project(config=models(p={"api_key_env": "LEAK"}))
    (root / ".env").write_text(f"LEAK={SECRET}\n", encoding="utf-8")
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert "LEAK" in result.out  # the variable is unset as far as the check can see
    assert SECRET not in result.both
    with pytest.raises(UntrustedProject, match="tau trust") as caught:
        load_config(root, world.environ, ask=None)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("A=1\nnot a pair\n", 2),
        ("A=1\n\n1BAD=x\n", 3),
        ("A='open", 1),
        ("A='x' y", 1),
        (r'A="bad \q escape"', 1),
    ],
)
def test_a_malformed_line_names_file_and_line_and_never_the_value(
    world: World, text: str, line: int
) -> None:
    root = trusted_project(world)
    path = root / ".env"
    path.write_text(text + f"\n# {SECRET}\n", encoding="utf-8")
    with pytest.raises(DotenvError) as caught:
        load_config(root, world.environ, ask=None)
    assert str(caught.value).startswith(f"{path}:{line}:")
    checked = tau(world, "config", "check", cwd=root)
    assert checked.code == 1
    assert f"{path}: error: " in checked.out or f"{path}:{line}:" in checked.out
    assert "Traceback" not in checked.err


def test_a_secret_on_a_malformed_line_is_not_echoed(world: World) -> None:
    root = trusted_project(world)
    (root / ".env").write_text(f"{SECRET} oops\n", encoding="utf-8")
    with pytest.raises(DotenvError) as caught:
        load_config(root, world.environ, ask=None)
    assert SECRET not in str(caught.value)
    assert SECRET not in tau(world, "config", "check", cwd=root).both
