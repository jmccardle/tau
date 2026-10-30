"""Reading a project's `.env` file (D13): our own reader, one parse everywhere.

Lines are `KEY=value`, optionally led by `export `. Blank lines and `#` lines
are skipped. A single-quoted value is literal; a double-quoted value takes the
escapes `\\n`, `\\"`, and `\\\\`; an unquoted value is trimmed and loses a
trailing ` # comment` (a `#` starts a comment only at the line start
or after whitespace; a BOM is tolerated). Anything else is an error naming the file and line,
never the value.
"""

import re

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPORT = re.compile(r"export\s+")
_COMMENT = re.compile(r"\s#.*$")
_ESCAPES = {"n": "\n", '"': '"', "\\": "\\"}


class DotenvError(ValueError):
    """A `.env` line that is not `KEY=value`; the message names the file and line, not the value."""


def _quoted(rest: str, where: str) -> str:
    quote = rest[0]
    out: list[str] = []
    i = 1
    while i < len(rest):
        c = rest[i]
        if c == quote:
            tail = rest[i + 1 :]
            if tail.strip() and not (tail[0].isspace() and tail.strip().startswith("#")):
                raise DotenvError(f"{where}: text after the closing {quote}")
            return "".join(out)
        if c == "\\" and quote == '"':
            i += 1
            if i >= len(rest) or rest[i] not in _ESCAPES:
                raise DotenvError(f"{where}: unknown escape in a double-quoted value")
            c = _ESCAPES[rest[i]]
        out.append(c)
        i += 1
    raise DotenvError(f"{where}: unterminated {quote} quote")


def parse_dotenv(text: str, source: str = ".env") -> dict[str, str]:
    """The variables in `text`; a later line for the same key wins.

    Raises `DotenvError` naming `source` and the line number.
    """
    found: dict[str, str] = {}
    for number, raw in enumerate(text.removeprefix("\ufeff").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{source}:{number}"
        if (m := _EXPORT.match(line)) is not None:
            line = line[m.end() :]
        key, equals, rest = line.partition("=")
        key = key.strip()
        if not equals:
            raise DotenvError(f"{where}: expected KEY=value")
        if not _KEY.fullmatch(key):
            raise DotenvError(f"{where}: invalid variable name")
        if rest.lstrip()[:1] in ("'", '"'):
            found[key] = _quoted(rest.lstrip(), where)
        else:
            found[key] = _COMMENT.sub("", rest).strip()
    return found
