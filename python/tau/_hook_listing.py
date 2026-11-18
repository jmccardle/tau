"""What `tau ext list --hooks` prints about hook order (D28 section 5):
one line per hook point that has a hook, its extensions in the order they run."""

import json
from collections.abc import Sequence

from tau._config_layers import option_problems
from tau._extensions import inspect_extensions
from tau._hook_order import HookOrder
from tau._startup import LoadedConfig, off_keys


def hook_lines(table: HookOrder) -> list[str]:
    """`pre_sample  usage -100, agents 0`: a line per point with hooks, the points aligned, an
    order the config set marked `(config)`."""
    rows = {point: placed for point, placed in table.points.items() if placed}
    width = max((len(p) for p in rows), default=0)
    return [
        f"{point:<{width}}  "
        + ", ".join(
            f"{p.extension.name} {p.order}" + (" (config)" if p.configured else "") for p in placed
        )
        for point, placed in rows.items()
    ]


def hook_json(table: HookOrder) -> list[str]:
    """One JSON object per point with hooks: `{"point": ..., "hooks": [{"extension": ...,
    "order": ..., "configured": ...}]}`."""
    return [
        json.dumps(
            {
                "point": point,
                "hooks": [
                    {"extension": p.extension.name, "order": p.order, "configured": p.configured}
                    for p in placed
                ],
            }
        )
        for point, placed in table.points.items()
        if placed
    ]


def ext_hooks(
    loaded: LoadedConfig, explicit: Sequence[str], discover: bool, *, as_json: bool
) -> tuple[list[str], int]:
    """`tau ext list --hooks`: the lines to print and the exit status. What loads is what
    `inspect_extensions` decides; a source that failed or a bad configured order is an error
    (the message `tau config check` gives), and the table is not printed."""
    result = inspect_extensions(loaded, explicit=explicit, discover=discover)
    errors = [*result.errors]
    errors += [
        str(p)
        for p in option_problems(loaded.catalog, result.extensions, off_keys(result.sources))
        if p.severity == "error"
    ]
    if errors:
        return errors, 1
    table = HookOrder.of(result.extensions, loaded.catalog.orders)
    return (hook_json(table) if as_json else hook_lines(table)), 0
