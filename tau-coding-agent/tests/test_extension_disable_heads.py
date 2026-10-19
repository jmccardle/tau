"""#58 in the heads: ``tau ext disable|enable``, and the core's load-failure notice.

Reference: docs/EXTENSION-PACKAGES.md (the #58 note). The conftest points the
default extensions directory at a fresh τ home, so nothing here touches the real one.
"""

from __future__ import annotations

import json

import pytest

from tau_agent_core import extension_sources
from tau_agent_core.extension_auth import STORE_FILENAME
from tau_agent_core.extension_sources import ExtensionSource
from tau_agent_core.sdk import ExtensionLoadError, LoadExtensionsResult, load_failure_notice
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.backends import TauBackend, create_backend
from tau_coding_agent.cli import CLIArgs, main
from tau_coding_agent.headless import run_print

from repl_fakes import ReplEnv, env  # noqa: F401

_BROKEN = "raise RuntimeError('broken on import')\n"
_NOTICE = (
    "extension broken ({path}) failed to load: broken on import. "
    "To stop loading it: tau ext disable broken"
)


@pytest.fixture
def ext_dir():
    """The hermetic extensions directory, holding a broken drop-in authorized at upgrade."""
    directory = extension_sources.default_extensions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.py").write_text(_BROKEN)
    return directory


def _config() -> dict:
    return {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "You are helpful.",
    }


@pytest.fixture
def no_model(monkeypatch):
    """A turn that never reaches a provider."""

    async def _fake_stream(self, submission, context, callback, on_event=None, on_pi_event=None):
        return (
            "",
            {},
            [],
            [],
            SubmissionResult(accepted=True, submission_id=submission.submission_id),
        )

    monkeypatch.setattr(TauBackend, "stream_submission", _fake_stream)


class TestTheCli:
    def test_disable_list_enable(self, ext_dir, capsys):
        assert main(["ext", "list"]) == 0
        capsys.readouterr()
        assert main(["ext", "disable", "broken"]) == 0
        assert "disabled broken" in capsys.readouterr().out
        main(["ext", "list", "--json"])
        [row] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert (row["identifier"], row["state"]) == ("broken", "disabled")
        assert "tau ext enable broken" in row["reason"]

        assert main(["ext", "enable", "broken"]) == 0
        assert "enabled broken" in capsys.readouterr().out
        main(["ext", "list"])
        assert capsys.readouterr().out.startswith("broken  authorized")

    def test_an_unknown_name_exits_2(self, ext_dir, capsys):
        assert main(["ext", "disable", "nope"]) == 2
        assert "no discovered or recorded extension is named 'nope'" in capsys.readouterr().err
        assert main(["ext", "enable", "broken"]) == 2
        assert "no disabled extension" in capsys.readouterr().err

    def test_help_names_both_verbs(self, capsys):
        with pytest.raises(SystemExit):
            main(["ext", "--help"])
        out = capsys.readouterr().out
        assert "disable" in out and "enable" in out


class TestPrintMode:
    async def test_a_failing_discovered_extension_is_one_notice_and_the_run_goes_on(
        self, ext_dir, no_model, capsys
    ):
        rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, model="m"), _config())
        assert rc == 0
        err = capsys.readouterr().err
        assert f"[τ] {_NOTICE.format(path=ext_dir / 'broken.py')}" in err.splitlines()

    async def test_disabled_says_nothing(self, ext_dir, no_model, capsys):
        assert main(["ext", "list"]) == 0
        assert main(["ext", "disable", "broken"]) == 0
        capsys.readouterr()
        rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, model="m"), _config())
        assert rc == 0
        assert "broken" not in capsys.readouterr().err


class TestTheTui:
    @pytest.fixture
    def app(self, make_app):
        return make_app(create_backend=create_backend, discover_extensions=True)

    async def _notices(self, app, monkeypatch) -> list[tuple[str, str]]:
        notices: list[tuple[str, str]] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            monkeypatch.setattr(
                app, "notify", lambda msg, **kw: notices.append((msg, kw.get("severity", "")))
            )
            await app.action_new_chat()
            await pilot.pause()
        return notices

    async def test_a_failing_discovered_extension_is_the_cores_notice(
        self, app, ext_dir, monkeypatch
    ):
        notices = await self._notices(app, monkeypatch)
        assert (_NOTICE.format(path=ext_dir / "broken.py"), "warning") in notices

    async def test_disabled_says_nothing(self, app, ext_dir, monkeypatch):
        (ext_dir.parent / STORE_FILENAME).write_text('{"format": 1}')
        assert main(["ext", "disable", "broken"]) == 0
        notices = await self._notices(app, monkeypatch)
        assert not any("broken" in msg for msg, _ in notices)


async def test_the_repl_prints_the_cores_notice(env: ReplEnv) -> None:
    error = ExtensionLoadError(
        path="/x/broken.py",
        error="boom",
        source=ExtensionSource(identifier="broken", kind="dropin", target="/x/broken.py"),
    )

    def prepare(backend) -> None:
        async def load(*_args, **_kwargs):
            return LoadExtensionsResult(errors=[error])

        backend.load_extensions = load

    env.install(prepare)
    await env.run([])
    assert " ".join(load_failure_notice(error).split()) in " ".join(env.text.split())
