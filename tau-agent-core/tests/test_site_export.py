"""Contract tests for the documentation-site exporter.

Reference: docs/AGENT-DOCS.md; scripts/export_site_reference.py

The exported pages live in another repository, so nothing here can assert that
a particular site checkout is current -- ``--check`` is what does that, in that
checkout. What is held here is everything the exporter promises before it ever
touches a site:

1. A site page's body is byte-identical to the ``docs/library/reference/`` page
   for the same topic. One renderer, two heads.
2. The re-heading refuses a ``render_topic`` output it does not recognise,
   rather than slicing the wrong lines off a changed shape.
3. The nav lines and the written files name the same set of pages, so an export
   cannot leave a page in no menu.
4. ``splice_nav`` changes only the marked region, and refuses a file with no
   markers.
5. The stale sweep refuses to delete a file the exporter did not write.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tau_agent_core.docs_build import PACKAGES, collect, pages, render_topic, source_paths

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "export_site_reference.py"


def _load_script() -> ModuleType:
    """Import the exporter by path -- ``scripts/`` is not an installed package."""
    spec = importlib.util.spec_from_file_location("export_site_reference", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export = _load_script()


@pytest.fixture(scope="module")
def built() -> list:
    """The topic pages for this checkout, collected once."""
    objects, _warnings = collect(PACKAGES, source_paths(REPO_ROOT))
    return pages(objects)


def test_the_body_survives_the_re_heading(built: list) -> None:
    for page in built:
        library = render_topic(page)
        site = export.site_page(page, library)
        assert library.split("\n")[4:] == site.split("\n")[8:], page.topic


def test_the_re_heading_refuses_an_unrecognised_shape(built: list) -> None:
    with pytest.raises(export.ExportError, match="no longer produces"):
        export.site_page(built[0], "# Title\n\nnot the banner\n\nbody\n")


def test_the_nav_names_exactly_the_pages_written() -> None:
    files, nav = export.render_all(REPO_ROOT)
    listed = {line.split("/")[-1] for line in nav if line.strip().endswith(".md")}
    assert listed == set(files)


def test_the_nav_splice_touches_only_the_marked_region() -> None:
    before = f"a: 1\n    {export.NAV_BEGIN}\n    - old\n    {export.NAV_END}\nb: 2\n"

    after = export.splice_nav(before, ["- API:", "    - one.md"])

    assert after == (
        f"a: 1\n    {export.NAV_BEGIN}\n    - API:\n        - one.md\n    {export.NAV_END}\nb: 2\n"
    )


def test_the_nav_splice_refuses_a_file_with_no_markers() -> None:
    with pytest.raises(export.ExportError, match="exactly one marker pair"):
        export.splice_nav("nav:\n  - index.md\n", ["- API:"])


def test_the_stale_sweep_refuses_a_file_it_did_not_write(tmp_path: Path) -> None:
    (tmp_path / "mine.md").write_text(f"{export.SITE_BANNER}\n")
    (tmp_path / "theirs.md").write_text("hand-written\n")

    with pytest.raises(export.ExportError, match="refusing to delete"):
        export._stale(tmp_path, set())


def test_the_stale_sweep_returns_its_own_orphans(tmp_path: Path) -> None:
    (tmp_path / "gone.md").write_text(f"{export.SITE_BANNER}\n")
    (tmp_path / "kept.md").write_text(f"{export.SITE_BANNER}\n")

    assert export._stale(tmp_path, {"kept.md"}) == [tmp_path / "gone.md"]


def test_a_path_that_is_not_the_site_is_refused(tmp_path: Path) -> None:
    with pytest.raises(export.ExportError, match="is not the documentation site"):
        export.resolve_site(tmp_path)
