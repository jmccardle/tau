"""The `.env` reader (D13): our own parse, errors name file and line, never the value."""

from pathlib import Path

import pytest

from tau_next import DotenvError, parse_dotenv, read_dotenv


def test_plain_lines_comments_and_export() -> None:
    text = "# a comment\n\nA=1\nexport B=two\n  C = three  \nexport   D=4\n"
    assert parse_dotenv(text) == {"A": "1", "B": "two", "C": "three", "D": "4"}


def test_single_quotes_are_literal() -> None:
    assert parse_dotenv(r"A='x\ny # not a comment $HOME'") == {"A": r"x\ny # not a comment $HOME"}


def test_double_quotes_take_three_escapes() -> None:
    assert parse_dotenv(r'A="line\nnext \"q\" back\\slash"') == {"A": 'line\nnext "q" back\\slash'}


def test_unquoted_value_loses_trailing_comment_only() -> None:
    parsed = parse_dotenv("A=value # note\nB=a#b\nC=\nD=   # only a note\n")
    assert parsed == {"A": "value", "B": "a#b", "C": "", "D": ""}


def test_a_comment_may_follow_a_quoted_value() -> None:
    assert parse_dotenv("A='x' # why\nB=\"y\"#z") == {"A": "x", "B": "y"}


def test_value_may_contain_equals() -> None:
    assert parse_dotenv("URL=http://h/?a=b") == {"URL": "http://h/?a=b"}


def test_a_later_line_wins() -> None:
    assert parse_dotenv("A=1\nA=2") == {"A": "2"}


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("A=1\nnot a pair\n", 2),
        ("A=1\n\n1BAD=x\n", 3),
        ("A-B=x", 1),
        ("=x", 1),
        ("A='open", 1),
        ('A="open', 1),
        ("A='x' y", 1),
        (r'A="bad \q escape"', 1),
        ('A="trailing \\', 1),
    ],
)
def test_malformed_lines_name_file_and_line_and_never_the_value(text: str, line: int) -> None:
    with pytest.raises(DotenvError) as caught:
        parse_dotenv(text + "\n", "proj/.env")
    message = str(caught.value)
    assert message.startswith(f"proj/.env:{line}:")


def test_secret_value_is_not_echoed() -> None:
    with pytest.raises(DotenvError) as caught:
        parse_dotenv("sk-secret-123 oops", "f")
    assert "sk-secret" not in str(caught.value)


def test_read_dotenv_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\nbroken\n", encoding="utf-8")
    with pytest.raises(DotenvError, match=rf"{path}:2:"):
        read_dotenv(path)
    path.write_text("A=1\n", encoding="utf-8")
    assert read_dotenv(path) == {"A": "1"}
