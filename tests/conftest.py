"""Suite-wide enforcement of CLAUDE.md rule 4 (fakes at the effect boundary)."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    patched = [i.nodeid for i in items if "monkeypatch" in getattr(i, "fixturenames", ())]
    if patched:
        raise pytest.UsageError(
            "rule 4: `monkeypatch` is patching; use FakeProvider/FakeTool instead:\n  "
            + "\n  ".join(patched)
        )
