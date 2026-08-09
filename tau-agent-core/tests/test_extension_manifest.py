"""tau-006 — H7+H8: the extension boundary declares what it is, and is checked
against it before it runs (SIM_SPEC_v2 §16.6, §16.10).

H7 gives ``ExtensionInfo`` two more fields — ``subjects`` (the bus subjects an
extension declares it touches) and ``content_hash`` (the file's identity at
load time) — and makes ``subjects`` a required declaration for any extension
that sets ``TOUCHES_BUS = True``: omitting it is a load error, not a hole in
the manifest.

H8 checks that declaration against the session's actual bus capability AT THE
FACTORY, before ``register(api)`` runs, and refuses (never warns-and-continues)
when the session has no bus transport to back it.

These load REAL file extensions through ``AgentSession.load_extensions`` (the
live-bind seam ``test_extensions_summary.py`` already established), not
hand-built structs — a regression in the factory preflight ordering fails here.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.run_manifest import build_run_manifest, extension_manifest_entries
from tau_agent_core.sdk import ExtensionCapabilityError, summarize_extensions
from tau_agent_core.compaction_policy import CompactionPolicy
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _make_session(*, bus_available: bool = False) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(), model=_model(), bus_available=bus_available
    )


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


# A plain extension: no bus declaration at all. Must load under either setting.
_PLAIN_EXT = """
def register(api):
    api.register_command("hello", {"description": "say hi"})
"""

# A bus-touching extension with a valid declaration.
_BUS_EXT = """
TOUCHES_BUS = True
SUBJECTS = ("events.workspace.demo.out.say", "events.workspace.demo.in.>")

def register(api):
    api.register_command("speak", {"description": "publish a demo event"})
"""

# Declares itself bus-touching but supplies no SUBJECTS at all (H7 omission).
_BUS_EXT_NO_SUBJECTS = """
TOUCHES_BUS = True

def register(api):
    api.register_command("speak", {"description": "publish a demo event"})
"""

# Declares itself bus-touching with an explicitly empty SUBJECTS (H7 omission,
# the other spelling of "forgot to fill this in").
_BUS_EXT_EMPTY_SUBJECTS = """
TOUCHES_BUS = True
SUBJECTS = ()

def register(api):
    pass
"""


class TestSubjectsIsRequiredForABusTouchingExtension:
    """H7: omitting ``subjects`` fails the load, it does not produce a hole."""

    async def test_touches_bus_with_no_subjects_attr_fails_load(self, tmp_path):
        ext = _write(tmp_path / "bus_ext.py", _BUS_EXT_NO_SUBJECTS)
        session = _make_session(bus_available=True)

        with pytest.raises(ExtensionCapabilityError, match="SUBJECTS"):
            await session.load_extensions([str(ext)], discover=False)

    async def test_touches_bus_with_empty_subjects_fails_load(self, tmp_path):
        ext = _write(tmp_path / "bus_ext.py", _BUS_EXT_EMPTY_SUBJECTS)
        session = _make_session(bus_available=True)

        with pytest.raises(ExtensionCapabilityError, match="SUBJECTS"):
            await session.load_extensions([str(ext)], discover=False)

    async def test_omission_never_reaches_register(self, tmp_path):
        """A refused declaration means register() never ran — no partial side effect."""
        ext = _write(tmp_path / "bus_ext.py", _BUS_EXT_NO_SUBJECTS)
        session = _make_session(bus_available=True)

        with pytest.raises(ExtensionCapabilityError):
            await session.load_extensions([str(ext)], discover=False)

        # The command register() would have added is simply absent — not a
        # rollback of a partial registration, because none ever started.
        assert session.list_managed_extensions() == []

    async def test_valid_declaration_loads_and_reports_subjects(self, tmp_path):
        ext = _write(tmp_path / "bus_ext.py", _BUS_EXT)
        session = _make_session(bus_available=True)

        result = await session.load_extensions([str(ext)], discover=False)
        infos = summarize_extensions(result)

        assert len(infos) == 1
        assert infos[0].subjects == (
            "events.workspace.demo.out.say",
            "events.workspace.demo.in.>",
        )

    async def test_plain_extension_needs_no_declaration(self, tmp_path):
        """An extension that never claims TOUCHES_BUS is unaffected either way."""
        ext = _write(tmp_path / "plain_ext.py", _PLAIN_EXT)
        session = _make_session(bus_available=False)

        result = await session.load_extensions([str(ext)], discover=False)
        infos = summarize_extensions(result)

        assert infos[0].subjects == ()
        assert infos[0].commands == ["hello"]


class TestCapabilityPreflightRefusesAtTheFactory:
    """H8: a bus-touching extension is refused, not warned-and-continued, when
    the session has no bus transport to back the declared capability."""

    async def test_refused_when_session_has_no_bus(self, tmp_path):
        ext = _write(tmp_path / "bus_ext.py", _BUS_EXT)
        session = _make_session(bus_available=False)

        with pytest.raises(ExtensionCapabilityError, match="bus_available=False"):
            await session.load_extensions([str(ext)], discover=False)

        # Refused, not degraded: nothing got registered, and it is not sitting
        # in the loaded-extensions map under some "disabled" half-state.
        assert session.list_managed_extensions() == []

    async def test_loads_when_session_declares_bus_available(self, tmp_path):
        ext = _write(tmp_path / "bus_ext.py", _BUS_EXT)
        session = _make_session(bus_available=True)

        result = await session.load_extensions([str(ext)], discover=False)

        assert len(result.extensions) == 1
        assert len(result.errors) == 0

    async def test_discovered_bus_extension_refusal_is_a_collected_error_not_a_raise(
        self, tmp_path
    ):
        """A *discovered* (not explicit -e) refusal follows the existing discovered-
        error policy: collected into result.errors, not raised past the loader —
        refusal still means "did not run", it does not change the explicit/discovered
        contract this loader already has."""
        ext_dir = tmp_path / "extdir"
        ext_dir.mkdir()
        _write(ext_dir / "bus_ext.py", _BUS_EXT)
        session = _make_session(bus_available=False)

        result = await session.load_extensions(
            None, discover=True, user_dir=str(ext_dir)
        )

        assert result.extensions == []
        assert len(result.errors) == 1
        assert "bus_available=False" in result.errors[0].error


class TestContentIdentityIsNotJustPath:
    """H7: two runs against the same path at different contents are two
    conditions carrying one label unless content_hash tells them apart."""

    async def test_reload_after_edit_changes_the_hash(self, tmp_path):
        ext = _write(tmp_path / "versioned_ext.py", _PLAIN_EXT)
        session = _make_session()

        result_v1 = await session.load_extensions([str(ext)], discover=False)
        hash_v1 = summarize_extensions(result_v1)[0].content_hash
        assert hash_v1  # never empty for a real file

        # Same path, different content — the case §16.6 names by name. Reload
        # (rather than a second load_extensions call) is the realistic path: it
        # re-imports the file fresh, which is exactly what must re-hash.
        _write(ext, _PLAIN_EXT + "\n# a harmless edit\n")
        action = await session.reload_extension(str(ext))
        assert action.ok

        hash_v2 = session._loaded_extensions[str(ext)].content_hash
        assert hash_v2 != hash_v1

    async def test_two_extensions_same_content_share_a_hash(self, tmp_path):
        """Not path-derived: identical bytes hash identically regardless of path."""
        ext_a = _write(tmp_path / "a.py", _PLAIN_EXT)
        ext_b = _write(tmp_path / "b.py", _PLAIN_EXT)
        session = _make_session()

        result = await session.load_extensions([str(ext_a), str(ext_b)], discover=False)
        infos = summarize_extensions(result)

        assert infos[0].content_hash == infos[1].content_hash
        assert infos[0].path != infos[1].path


class TestManifestEmitsExtensionsBesideHarness:
    """H7 DoD: content hash/version + subjects, emitted to manifest.json beside
    ``harness`` — same idiom H5 established for ``compaction``."""

    async def test_extension_manifest_entries_shape(self, tmp_path):
        ext = _write(tmp_path / "bus_ext.py", _BUS_EXT)
        session = _make_session(bus_available=True)
        result = await session.load_extensions([str(ext)], discover=False)
        infos = summarize_extensions(result)

        entries = extension_manifest_entries(infos)

        assert entries == [
            {
                "name": "bus_ext",
                "path": str(ext),
                "content_hash": infos[0].content_hash,
                "subjects": [
                    "events.workspace.demo.out.say",
                    "events.workspace.demo.in.>",
                ],
                "tools": [],
                "commands": ["speak"],
                "shortcuts": [],
                "hooks": [],
            }
        ]

    async def test_build_run_manifest_carries_extensions_beside_compaction(self, tmp_path):
        ext = _write(tmp_path / "plain_ext.py", _PLAIN_EXT)
        session = _make_session()
        result = await session.load_extensions([str(ext)], discover=False)
        infos = summarize_extensions(result)

        manifest = build_run_manifest(
            compaction_policy=CompactionPolicy.disabled(max_turns=10),
            extensions=infos,
        )

        assert manifest["harness"] == "tau"
        assert "compaction" in manifest
        assert manifest["extensions"][0]["name"] == "plain_ext"

    def test_extensions_key_omitted_when_not_supplied(self):
        """None (the default) omits the key rather than fabricating an empty list —
        distinguishing "no extensions were loaded" from "nobody threaded this yet"."""
        manifest = build_run_manifest(compaction_policy=CompactionPolicy.disabled(max_turns=1))
        assert "extensions" not in manifest

    def test_compaction_still_refused_through_extra_with_extensions_present(self):
        """Regression: adding the ``extensions`` parameter must not loosen the
        pre-existing ``compaction`` owned-key refusal (H5)."""
        with pytest.raises(ValueError, match="compaction"):
            build_run_manifest(
                compaction_policy=CompactionPolicy.disabled(max_turns=1),
                extensions=[],
                **{"compaction": {"already": "here"}},
            )


class TestSummarizeExtensionsNoRunnerBucketRaiseIsIntact:
    """Regression guard: H7/H8 must not weaken the pre-existing Fail-Early raise
    ``summarize_extensions`` performs on an unbound api (E5.4/S34)."""

    def test_unbound_api_still_raises(self):
        from tau_agent_core.extension_types import ExtensionAPI
        from tau_agent_core.sdk import LoadedExtension, LoadExtensionsResult

        unbound_api = ExtensionAPI(hook_handlers=None)
        result = LoadExtensionsResult(
            extensions=[LoadedExtension(path="/nowhere.py", register=lambda api: None, api=unbound_api)]
        )

        with pytest.raises(RuntimeError, match="no runner bucket"):
            summarize_extensions(result)
