"""A persistent disable, and one load-failure notice that says how to set it (#58).

Reference: docs/EXTENSION-PACKAGES.md §5 and the after-the-build note for #58.
A discovered extension that fails to load gets a notice and the run goes on;
``tau ext disable`` silences it; ``-e`` overrides the disable and stays fatal.
"""

from __future__ import annotations

import json

import pytest

from tau_agent_core.extension_auth import (
    STORE_FILENAME,
    AuthorizationStore,
    AuthorizationStoreError,
)
from tau_agent_core.extension_sources import ExtensionSource
from tau_agent_core.sdk import (
    ExtensionLoadError,
    ExtensionStartupError,
    _load_extensions,
    disable_discovered_extension,
    enable_discovered_extension,
    extension_records,
    load_failure_notice,
    noticed_withheld,
    withheld_notices,
)

_DROPIN_OK = "def register(api):\n    pass\n"
_BROKEN = "raise RuntimeError('broken on import')\n"
_MARKER = "import pathlib\npathlib.Path({marker!r}).write_text('imported')\ndef register(api):\n    pass\n"


@pytest.fixture
def home(tmp_path):
    """A τ home with an extensions directory and an existing (empty) store."""
    directory = tmp_path / "extensions"
    directory.mkdir()
    (tmp_path / STORE_FILENAME).write_text('{"format": 1}')
    return tmp_path


def _authorized_dropin(home, stem: str, source: str) -> str:
    """Write a drop-in and authorize it; return its path."""
    path = home / "extensions" / f"{stem}.py"
    path.write_text(source)
    store = AuthorizationStore.load(home / STORE_FILENAME)
    store.authorize(ExtensionSource(identifier=stem, kind="dropin", target=str(path)))
    store.save()
    return str(path)


def _disable(home, token: str) -> tuple[str, bool]:
    return disable_discovered_extension(token, extensions_dir=home / "extensions")


class TestTheNotice:
    def test_a_discovered_failure_says_how_to_disable_it(self):
        error = ExtensionLoadError(
            path="/x/broken.py",
            error="boom",
            source=ExtensionSource(identifier="broken", kind="dropin", target="/x/broken.py"),
        )
        assert load_failure_notice(error) == (
            "extension broken (/x/broken.py) failed to load: boom. "
            "To stop loading it: tau ext disable broken"
        )

    def test_a_package_names_its_module_once(self):
        error = ExtensionLoadError(
            path="acme.pirate",
            error="ImportError: x",
            source=ExtensionSource(identifier="acme.pirate", kind="package", target="acme.pirate"),
        )
        assert load_failure_notice(error) == (
            "extension acme.pirate failed to load: ImportError: x. "
            "To stop loading it: tau ext disable acme.pirate"
        )

    def test_an_explicit_failure_says_to_drop_the_flag(self):
        error = ExtensionLoadError(path="/x/mine.py", error="boom", explicit=True)
        assert load_failure_notice(error).endswith(
            "It was named with -e; drop that to stop loading it"
        )


class TestDisable:
    async def test_a_failing_discovered_extension_is_noticed_and_the_load_goes_on(self, home):
        _authorized_dropin(home, "broken", _BROKEN)
        result = await _load_extensions(None, user_dir=str(home / "extensions"))
        [error] = result.errors
        assert load_failure_notice(error).endswith("To stop loading it: tau ext disable broken")

    async def test_disabled_is_never_imported_and_never_noticed(self, home, tmp_path):
        marker = tmp_path / "imported.txt"
        _authorized_dropin(home, "quiet", _MARKER.format(marker=str(marker)))
        _authorized_dropin(home, "broken", _BROKEN)
        assert _disable(home, "quiet") == ("quiet", True)
        assert _disable(home, "broken") == ("broken", True)

        result = await _load_extensions(None, user_dir=str(home / "extensions"))

        assert not marker.exists()
        assert result.errors == [] and result.extensions == []
        assert {(r.identifier, r.state) for r in result.withheld} == {
            ("quiet", "disabled"),
            ("broken", "disabled"),
        }
        assert noticed_withheld(result.withheld) == []
        assert withheld_notices(result) == []

    def test_the_listing_shows_disabled_with_how_to_undo_it(self, home):
        _authorized_dropin(home, "quiet", _DROPIN_OK)
        _disable(home, "quiet")
        [row] = extension_records(extensions_dir=home / "extensions")
        assert row.state == "disabled"
        assert row.reason is not None and "tau ext enable quiet" in row.reason

    async def test_enable_restores_it(self, home, tmp_path):
        marker = tmp_path / "imported.txt"
        _authorized_dropin(home, "quiet", _MARKER.format(marker=str(marker)))
        _disable(home, "quiet")
        assert enable_discovered_extension("quiet", extensions_dir=home / "extensions") == "quiet"

        result = await _load_extensions(None, user_dir=str(home / "extensions"))

        assert marker.exists()
        assert [x.source.identifier for x in result.extensions if x.source] == ["quiet"]

    async def test_an_unauthorized_disabled_drop_in_is_silent_too(self, home):
        (home / "extensions" / "stranger.py").write_text(_DROPIN_OK)
        _disable(home, "stranger")
        result = await _load_extensions(None, user_dir=str(home / "extensions"))
        assert [r.state for r in result.withheld] == ["disabled"]
        assert withheld_notices(result) == []

    async def test_e_overrides_a_disable(self, home, tmp_path):
        marker = tmp_path / "imported.txt"
        path = _authorized_dropin(home, "quiet", _MARKER.format(marker=str(marker)))
        _disable(home, "quiet")

        result = await _load_extensions([path], user_dir=str(home / "extensions"))

        assert marker.exists()
        assert [x.explicit for x in result.extensions] == [True]
        assert result.withheld == []

    async def test_e_stays_fatal_when_disabled(self, home):
        path = _authorized_dropin(home, "broken", _BROKEN)
        _disable(home, "broken")
        with pytest.raises(ExtensionStartupError, match="named with -e, failed at load"):
            await _load_extensions([path], user_dir=str(home / "extensions"))


class TestNaming:
    def test_an_unknown_name_is_refused_and_nothing_is_written(self, home):
        before = (home / STORE_FILENAME).read_text()
        with pytest.raises(ValueError, match="no discovered or recorded extension"):
            _disable(home, "nope")
        assert (home / STORE_FILENAME).read_text() == before

    def test_a_shorthand_resolves_to_the_full_identifier(self, home):
        (home / "extensions" / "acme.pirate.py").write_text(_DROPIN_OK)
        assert _disable(home, "pirate") == ("acme.pirate", True)

    def test_an_ambiguous_shorthand_is_refused(self, home):
        (home / "extensions" / "a.pirate.py").write_text(_DROPIN_OK)
        (home / "extensions" / "b.pirate.py").write_text(_DROPIN_OK)
        with pytest.raises(ValueError, match="ambiguous"):
            _disable(home, "pirate")

    def test_a_drop_in_path_names_it(self, home):
        (home / "extensions" / "quiet.py").write_text(_DROPIN_OK)
        assert _disable(home, str(home / "extensions" / "quiet.py")) == ("quiet", True)

    def test_an_identifier_only_the_store_knows_can_be_disabled(self, home):
        store = AuthorizationStore.load(home / STORE_FILENAME)
        store.authorize(
            ExtensionSource(
                identifier="gone.ext", kind="package", target="gone.ext", distribution="gone"
            )
        )
        store.save()
        assert _disable(home, "gone.ext") == ("gone.ext", True)

    def test_disabling_twice_reports_no_change(self, home):
        (home / "extensions" / "quiet.py").write_text(_DROPIN_OK)
        _disable(home, "quiet")
        assert _disable(home, "quiet") == ("quiet", False)

    def test_enabling_what_is_not_disabled_is_refused(self, home):
        with pytest.raises(ValueError, match="no disabled extension"):
            enable_discovered_extension("quiet", extensions_dir=home / "extensions")


class TestStoreFormat:
    def test_a_store_written_before_disable_existed_loads_and_keeps_its_records(self, home):
        old = {
            "format": 1,
            "packages": {
                "acme.ext": {
                    "distribution": "acme",
                    "version": "1.0",
                    "digest": None,
                    "editable_path": None,
                    "authorized_at": "2026-10-18T00:00:00+00:00",
                }
            },
            "dropins": {
                str(home / "extensions" / "old.py"): {
                    "identifier": "old",
                    "authorized_at": "2026-10-18T00:00:00+00:00",
                    "grandfathered": True,
                }
            },
        }
        (home / STORE_FILENAME).write_text(json.dumps(old))
        store = AuthorizationStore.load(home / STORE_FILENAME)
        assert store.disabled_identifiers() == []
        store.disable("old")
        store.save()

        saved = json.loads((home / STORE_FILENAME).read_text())
        assert saved["format"] == 1
        assert saved["packages"] == old["packages"]
        assert saved["dropins"] == old["dropins"]
        assert list(saved["disabled"]) == ["old"]

    def test_a_malformed_disabled_section_is_refused(self, home):
        (home / STORE_FILENAME).write_text('{"format": 1, "disabled": ["old"]}')
        with pytest.raises(AuthorizationStoreError, match="'disabled' must map"):
            AuthorizationStore.load(home / STORE_FILENAME)


async def test_disabling_before_the_first_start_still_grandfathers_the_rest(tmp_path):
    """Creating the store is the upgrade marker, so ``disable`` grandfathers as a start would."""
    directory = tmp_path / "extensions"
    directory.mkdir()
    (directory / "keep.py").write_text(_DROPIN_OK)
    (directory / "quiet.py").write_text(_DROPIN_OK)
    assert disable_discovered_extension("quiet", extensions_dir=directory) == ("quiet", True)
    result = await _load_extensions(None, user_dir=str(directory))
    assert [x.source.identifier for x in result.extensions if x.source] == ["keep"]
    assert [(r.identifier, r.state) for r in result.withheld] == [("quiet", "disabled")]
