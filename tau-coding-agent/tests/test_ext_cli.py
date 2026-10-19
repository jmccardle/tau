"""``tau ext list|authorize|revoke`` — the gate's non-interactive head.

Reference: docs/EXTENSION-PACKAGES.md §7 and §9. The conftest points the
default extensions directory at a fresh τ home, so these tests read and write
only there.
"""

from __future__ import annotations

import json

from tau_agent_core import extension_sources
from tau_agent_core.extension_auth import STORE_FILENAME
from tau_coding_agent.cli import main

_DROPIN = "def register(api):\n    pass\n"


def _ext_dir():
    directory = extension_sources.default_extensions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_list_needs_no_session_and_writes_nothing(capsys):
    directory = _ext_dir()
    (directory / "old.py").write_text(_DROPIN)
    assert main(["ext", "list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("old  authorized  dropin ")
    assert not (directory.parent / STORE_FILENAME).exists()


def test_list_json_is_one_object_per_line(capsys):
    directory = _ext_dir()
    (directory.parent / STORE_FILENAME).write_text('{"format": 1}')
    (directory / "late.py").write_text(_DROPIN)
    assert main(["ext", "list", "--json"]) == 0
    [row] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert (row["identifier"], row["state"], row["authorization"]) == (
        "late",
        "unauthorized",
        "new",
    )


def test_authorize_then_list_then_revoke(capsys):
    directory = _ext_dir()
    (directory.parent / STORE_FILENAME).write_text('{"format": 1}')
    (directory / "late.py").write_text(_DROPIN)

    assert main(["ext", "authorize", "late"]) == 0
    assert "authorized late" in capsys.readouterr().out
    main(["ext", "list"])
    assert "late  authorized" in capsys.readouterr().out

    assert main(["ext", "revoke", "late"]) == 0
    main(["ext", "list"])
    assert "late  unauthorized" in capsys.readouterr().out


def test_authorizing_a_name_nothing_has_is_an_error(capsys):
    _ext_dir()
    assert main(["ext", "authorize", "ghost"]) == 2
    assert "no discovered extension is named 'ghost'" in capsys.readouterr().err


def test_an_unusable_stem_is_listed_with_how_to_fix_it(capsys):
    directory = _ext_dir()
    (directory / "a:b.py").write_text(_DROPIN)
    main(["ext", "list"])
    out = capsys.readouterr().out
    assert "a:b  unusable  dropin" in out and "rename the file" in out


def test_a_headless_head_names_the_command_and_never_prompts(capsys):
    """docs/EXTENSION-PACKAGES.md §9: -p and --mode rpc write a line and go on."""
    from tau_agent_core.extension_sources import ExtensionRecord
    from tau_agent_core.sdk import LoadExtensionsResult
    from tau_coding_agent.headless import report_extension_load

    record = ExtensionRecord(
        identifier="tau_router.planner",
        kind="package",
        target="tau_router.planner",
        state="unauthorized",
        reason="tau-router 0.2 has never been authorized",
        distribution="tau-router",
        version="0.2",
        authorization="new",
    )
    report_extension_load(LoadExtensionsResult(withheld=[record], grandfathered=["old"]))
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("[τ] authorized 1 existing drop-in extension(s) at upgrade: old.")
    assert err[1].endswith("To load it: tau ext authorize tau_router.planner")
