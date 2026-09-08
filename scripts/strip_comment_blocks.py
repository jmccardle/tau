"""Delete every multi-line `#` comment block in a tree.

A block is a run of two or more CONSECUTIVE own-line comments -- own-line
meaning the physical line holds nothing but the comment. Runs of one survive,
trailing comments on a code line survive, and every line of a run survives if
it is a tool directive (`# type:`, `# noqa`, `# fmt: off`, a shebang, a coding
line): those change what another program does, so they are code, not prose.

Comments are found with `tokenize`, so a `#` inside a string or a docstring is
never a candidate. Deleting a comment line cannot change the parse -- a comment
is never a statement, so no block can be emptied by this -- and the script
proves it per file by re-parsing and comparing the AST dumps before and after.

Usage:  python strip_comment_blocks.py <root> [<root>...] [--write | --check]

`--check` writes nothing and exits 1 if any block is found, which is what the
pre-commit hook runs. It can afford to be a hard gate where the docs-coverage
gate cannot: the src trees hold zero blocks today, so it passes on a clean tree
and only ever fails on something a commit just added.
"""

import ast
import io
import os
import re
import sys
import tokenize

SKIP_DIRS = {
    ".git", "venv", "__pycache__", ".ropeproject", ".mypy_cache",
    ".pytest_cache", "build", "dist", "node_modules", ".tox",
}

DIRECTIVE = re.compile(
    r"^#\s*(!|type:|noqa|pragma:|fmt:|isort:|mypy:|ruff:|flake8:|pylint:|nosec"
    r"|coding[:=]|-\*-)"
)


def own_line_comments(src: str) -> dict[int, str]:
    """line number -> comment text, for comments that own their whole line."""
    lines = src.splitlines()
    out: dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT:
            continue
        line = lines[tok.start[0] - 1]
        if line.lstrip().startswith("#"):
            out[tok.start[0]] = tok.string
    return out


def blocks(marks: dict[int, str]) -> list[list[int]]:
    runs: list[list[int]] = []
    for ln in sorted(marks):
        if runs and runs[-1][-1] == ln - 1:
            runs[-1].append(ln)
        else:
            runs.append([ln])
    return [r for r in runs if len(r) > 1]


def strip(src: str) -> tuple[str, int, int]:
    """Delete the multi-line comment blocks in ``src``.

    Deleting a block also merges the blank runs on either side of it, so the
    smaller of the two goes with the block and the file keeps the separation
    it already used. Without that, 326 runs of three or more blank lines
    appeared in trees that had none.

    Args:
        src: the file's text.

    Returns:
        ``(new_text, comment_lines_deleted, blocks_deleted)``.
    """
    marks = own_line_comments(src)
    runs = [
        [ln for ln in run if not DIRECTIVE.match(marks[ln].strip())]
        for run in blocks(marks)
    ]
    runs = [r for r in runs if r]
    doomed = {ln for run in runs for ln in run}
    if not doomed:
        return src, 0, 0
    lines = src.splitlines(keepends=True)

    n_comments = len(doomed)

    def blank(n: int) -> bool:
        return 1 <= n <= len(lines) and not lines[n - 1].strip()

    for run in runs:
        before = after = 0
        while blank(run[0] - 1 - before):
            before += 1
        while blank(run[-1] + 1 + after):
            after += 1
        for k in range(min(before, after)):
            doomed.add(run[-1] + 1 + k)

    kept = [t for i, t in enumerate(lines, start=1) if i not in doomed]
    return "".join(kept), n_comments, len(runs)


def main() -> None:
    args = sys.argv[1:]
    write = "--write" in args
    check = "--check" in args
    roots = [a for a in args if a not in ("--write", "--check")]
    if not roots:
        raise SystemExit(__doc__)
    if write and check:
        raise SystemExit("--write and --check are exclusive: one edits, one reports")

    files = 0
    total_lines = 0
    total_blocks = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                new, n_lines, n_blocks = strip(src)
                if not n_lines:
                    continue
                if ast.dump(ast.parse(new)) != ast.dump(ast.parse(src)):
                    raise SystemExit(f"{path}: AST changed -- refusing to write")
                files += 1
                total_lines += n_lines
                total_blocks += n_blocks
                print(f"{os.path.relpath(path, root)}: -{n_lines} lines in {n_blocks} blocks")
                if write:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(new)

    verb = "removed" if write else "would remove"
    print(f"\n{verb} {total_lines} comment lines in {total_blocks} blocks across {files} files")

    if check and total_blocks:
        raise SystemExit(
            f"{total_blocks} multi-line comment block(s) in {files} file(s). A run of "
            "two or more own-line comments is prose that outgrew its place: put it in "
            "the docstring, or in a docs/ page the docstring cites. A single-line "
            "comment stating a dependence or an assumption is fine, and a tool "
            "directive is code and is exempt."
        )


main()
