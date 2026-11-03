"""The stylesheet is structure only: colour lives in the theme palettes."""

import re
from pathlib import Path

TCSS = Path(__file__).parents[2] / "python/tau/tui/tau.tcss"

# Every CSS named colour a stylesheet could spell instead of using a $variable.
_NAMED_WORDS = (
    "aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue "
    "blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk "
    "crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki "
    "darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen "
    "darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue "
    "dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro "
    "ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo "
    "ivory khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral "
    "lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon "
    "lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow "
    "lime limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid "
    "mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise "
    "mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace "
    "olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise "
    "palevioletred papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red "
    "rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver "
    "skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle "
    "tomato turquoise violet wheat white whitesmoke yellow yellowgreen "
)
NAMED_COLOURS = _NAMED_WORDS.split()

COLOUR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(|(?<![\w$-])(?:"
    + "|".join(NAMED_COLOURS)
    + r")(?![\w-])"
)


def _declarations(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every line outside `/* ... */` comments."""
    stripped = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.S)
    return list(enumerate(stripped.splitlines(), start=1))


def test_stylesheet_has_no_colour_literals() -> None:
    assert TCSS.is_file(), f"{TCSS} is missing"
    offenders = [
        f"{TCSS.name}:{number}: {line.strip()}"
        for number, line in _declarations(TCSS.read_text())
        if COLOUR_LITERAL.search(line)
    ]
    assert not offenders, "colour literals belong in a theme palette:\n" + "\n".join(offenders)
