"""Extensions from installed packages, behind one authorization gate.

Reference: docs/EXTENSION-PACKAGES.md. Each class pins one numbered rule of
the record: finding is not loading (§5), the identifier is the module name or
the stem (§2), an explicitly named extension is authorized by being named
(§5), unusable stems are listed and not loaded (§2.1), collisions neither
block nor bypass (§5), drop-ins are grandfathered once (§5).

Packages here are real: a ``.dist-info`` directory written beside a package on
a temporary ``sys.path`` entry, so ``importlib.metadata`` finds them exactly as
it finds a pip install.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_auth import (
    STORE_FILENAME,
    AuthorizationStore,
    AuthorizationStoreError,
)
from tau_agent_core.extension_sources import (
    ENTRY_POINT_GROUP,
    discover_dropins,
    discover_packages,
    resolve_explicit,
)
from tau_agent_core.sdk import (
    _load_extensions,
    authorization_prompt,
    authorize_discovered_extension,
    extension_records,
    plan_extensions,
    revoke_extension_authorization,
)
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

_COUNTER = itertools.count()

_EXT_SOURCE = """
from .helper import TOOL_NAME

def register(api):
    api.register_command("hello", {"description": "hi", "handler": lambda a, c: "hi"})
    api.on("session_start", lambda event, ctx: STARTS.append(event["reason"]))

STARTS = []
"""

_DROPIN = """
def register(api):
    api.register_command("hello", {"description": "hi", "handler": lambda a, c: "hi"})
"""


def _record_line(root: Path, rel: str) -> str:
    """One RECORD line with a real sha256, as pip writes it."""
    data = (root / rel).read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{rel},sha256={digest},{len(data)}"


def _install(
    site: Path,
    *,
    top: str,
    dist: str = "tau-fake",
    version: str = "1.0",
    entry: str | None = None,
    editable: Path | None = None,
) -> str:
    """Write package ``top`` (with ``top.ext``) and a dist-info declaring it; return the module."""
    module = f"{top}.ext"
    pkg = site / top
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "helper.py").write_text("TOOL_NAME = 'x'\n")
    (pkg / "ext.py").write_text(_EXT_SOURCE)
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    (info / "entry_points.txt").write_text(f"[{ENTRY_POINT_GROUP}]\next = {entry or module}\n")
    if editable is not None:
        (info / "direct_url.json").write_text(
            json.dumps({"url": f"file://{editable}", "dir_info": {"editable": True}})
        )
    rels = [f"{top}/__init__.py", f"{top}/helper.py", f"{top}/ext.py"]
    record = [_record_line(site, rel) for rel in rels]
    record.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(record) + "\n")
    return module


def _install_editable(
    site: Path, *, top: str, checkout: Path, package_dir: Path, shape: str
) -> str:
    """An editable install as setuptools writes one: RECORD lists only a finder or a ``.pth``.

    ``shape`` is ``"finder"`` (``__editable___*_finder.py`` with the annotated
    ``MAPPING: dict[str, str] = …`` setuptools >= 69 writes), ``"finder-plain"``
    (``MAPPING = …``, as older setuptools writes), or ``"pth"`` (a ``.pth`` naming
    the directory that holds ``top``).
    """
    module = f"{top}.ext"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("")
    (package_dir / "ext.py").write_text(_DROPIN)
    info = site / "tau_fake_ed-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: tau-fake-ed\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text(f"[{ENTRY_POINT_GROUP}]\next = {module}\n")
    (info / "direct_url.json").write_text(
        json.dumps({"url": f"file://{checkout}", "dir_info": {"editable": True}})
    )
    if shape.startswith("finder"):
        rel = "__editable___tau_fake_ed_1_0_finder.py"
        annotation = "" if shape == "finder-plain" else ": dict[str, str]"
        (site / rel).write_text(
            f"import sys\nMAPPING{annotation} = {{{top!r}: {str(package_dir)!r}}}\n"
        )
    else:
        rel = "__editable__.tau_fake_ed-1.0.pth"
        (site / rel).write_text(f"{package_dir.parent}\n")
    (info / "RECORD").write_text(f"{_record_line(site, rel)}\n{info.name}/RECORD,,\n")
    return module


SETUPTOOLS_FLOOR = 82
"""The setuptools the real-build tests need; tau-agent-core's ``[dev]`` extra declares it."""


def _require_setuptools() -> None:
    """Fail, naming the requirement, when this venv's setuptools is missing or too old.

    A failure rather than a skip: the release matrix installs ``[dev]`` fresh, and a
    skip would hide that it did not run there.
    """
    try:
        version = metadata.version("setuptools")
    except metadata.PackageNotFoundError:
        pytest.fail(
            f"this test builds editable wheels and needs setuptools>={SETUPTOOLS_FLOOR} "
            "in the test venv, which has none; install ffwf-tau-agent-core[dev]"
        )
    if int(version.split(".")[0]) < SETUPTOOLS_FLOOR:
        pytest.fail(
            f"this test needs setuptools>={SETUPTOOLS_FLOOR} in the test venv, found "
            f"{version}; install ffwf-tau-agent-core[dev]"
        )


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A fresh ``sys.path`` entry, and a unique top-level package name for this test."""
    root = tmp_path / "site"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    top = f"tauext_fake_{next(_COUNTER)}"
    yield root, top
    for name in [m for m in sys.modules if m == top or m.startswith(top + ".")]:
        del sys.modules[name]


@pytest.fixture
def home(tmp_path):
    """A τ home holding an empty extensions directory."""
    root = tmp_path / "home"
    (root / "extensions").mkdir(parents=True)
    return root


def _model() -> Model:
    return Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost:1/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model())


def _authorize_all(home: Path) -> None:
    """Grandfather nothing, then authorize every discovered source."""
    store = AuthorizationStore.load(home / STORE_FILENAME)
    for source in [*discover_dropins(home / "extensions"), *discover_packages()]:
        store.authorize(source)
    store.save()


class TestFindingIsNotLoading:
    def test_discovery_reads_metadata_and_imports_nothing(self, site, home):
        root, top = site
        module = _install(root, top=top)
        found = [s for s in discover_packages() if s.identifier == module]
        assert len(found) == 1
        source = found[0]
        assert (source.kind, source.distribution, source.version) == ("package", "tau-fake", "1.0")
        assert source.digest is not None and source.digest.startswith("sha256:")
        plan_extensions(extensions_dir=home / "extensions")
        assert top not in sys.modules

    async def test_an_unauthorized_package_is_withheld_and_offered(self, site, home):
        root, top = site
        module = _install(root, top=top)
        result = await _load_extensions(None, user_dir=str(home / "extensions"))
        assert result.extensions == []
        [record] = [r for r in result.withheld if r.identifier == module]
        assert (record.state, record.authorization) == ("unauthorized", "new")
        assert "tau-fake 1.0 has never been authorized" in record.reason
        assert top not in sys.modules

    async def test_an_authorized_package_loads_under_its_real_module_name(self, site, home):
        root, top = site
        module = _install(root, top=top)
        _authorize_all(home)
        session = _session()
        result = await session.load_extensions(None, user_dir=str(home / "extensions"))
        [loaded] = result.extensions
        assert loaded.path == module
        assert sys.modules[module].register is loaded.register, "real name, one module copy"
        assert f"ext:{module}:hello" in session._registry.get_commands()

    async def test_a_new_version_is_an_update_naming_the_old_one(self, site, home, tmp_path):
        root, top = site
        module = _install(root, top=top, version="1.0")
        _authorize_all(home)
        upgraded = tmp_path / "site2"
        _install(upgraded, top=top, version="2.0")
        sys.path.remove(str(root))
        sys.path.insert(0, str(upgraded))
        result = await _load_extensions(None, user_dir=str(home / "extensions"))
        [record] = [r for r in result.withheld if r.identifier == module]
        assert (record.authorization, record.previous_version) == ("update", "1.0")
        assert "changed from 1.0 to 2.0" in record.reason

    async def test_changed_files_at_the_same_version_say_so(self, site, home, tmp_path):
        root, top = site
        module = _install(root, top=top, version="1.0")
        _authorize_all(home)
        (root / top / "helper.py").write_text("TOOL_NAME = 'changed'\n")
        info = root / "tau_fake-1.0.dist-info"
        rels = [f"{top}/__init__.py", f"{top}/helper.py", f"{top}/ext.py"]
        lines = [_record_line(root, rel) for rel in rels] + [f"{info.name}/RECORD,,"]
        (info / "RECORD").write_text("\n".join(lines) + "\n")
        result = await _load_extensions(None, user_dir=str(home / "extensions"))
        [record] = [r for r in result.withheld if r.identifier == module]
        assert record.authorization == "update"
        assert "tau-fake 1.0's files changed since it was authorized" in record.reason
        assert "from 1.0 to 1.0" not in record.reason
        title, body = authorization_prompt(record)
        assert title == f"Load the updated extension {module}?"
        assert "tau-fake 1.0's installed files changed since you authorized it." in body
        assert "from 1.0 to 1.0" not in body

    def test_an_editable_install_is_keyed_to_its_path(self, site):
        root, top = site
        module = _install(root, top=top, editable=Path("/src/fake"))
        [source] = [s for s in discover_packages() if s.identifier == module]
        assert (source.editable_path, source.digest) == ("/src/fake", None)

    def test_an_entry_point_naming_an_attribute_is_unusable(self, site, home):
        root, top = site
        _install(root, top=top, entry=f"{top}.ext:register")
        [source] = [s for s in discover_packages() if s.identifier == f"{top}.ext"]
        assert source.unusable is not None and "names an attribute" in source.unusable
        plan = plan_extensions(extensions_dir=home / "extensions")
        assert [r.state for r in plan.withheld if r.identifier == source.identifier] == ["unusable"]


class TestDropInsShareTheGate:
    async def test_drop_ins_present_at_first_start_are_grandfathered_once(self, home):
        (home / "extensions" / "old.py").write_text(_DROPIN)
        first = await _load_extensions(None, user_dir=str(home / "extensions"))
        assert [Path(e.path).name for e in first.extensions] == ["old.py"]
        assert first.grandfathered == ["old"]
        assert (home / STORE_FILENAME).is_file()

        (home / "extensions" / "new.py").write_text(_DROPIN)
        second = await _load_extensions(None, user_dir=str(home / "extensions"))
        assert second.grandfathered == []
        assert [Path(e.path).name for e in second.extensions] == ["old.py"]
        assert [(r.identifier, r.state) for r in second.withheld] == [("new", "unauthorized")]

    async def test_an_explicit_path_is_authorized_by_being_named(self, home, tmp_path):
        AuthorizationStore.load(home / STORE_FILENAME).save()
        ext = tmp_path / "handset_bus.py"
        ext.write_text(_DROPIN)
        result = await _load_extensions(
            [str(ext)], discover=False, user_dir=str(home / "extensions")
        )
        assert [e.path for e in result.extensions] == [str(ext)]
        assert result.extensions[0].explicit

    async def test_no_extensions_writes_no_store(self, home):
        await _load_extensions(None, discover=False, user_dir=str(home / "extensions"))
        assert not (home / STORE_FILENAME).exists()

    def test_a_corrupt_store_is_refused(self, home):
        (home / STORE_FILENAME).write_text("{not json")
        with pytest.raises(AuthorizationStoreError, match="Fix or delete it"):
            plan_extensions(extensions_dir=home / "extensions")


class TestUnusableStems:
    async def test_a_colon_or_space_in_a_stem_is_listed_not_loaded(self, home):
        (home / "extensions" / "a:b.py").write_text(_DROPIN)
        (home / "extensions" / "c d.py").write_text(_DROPIN)
        result = await _load_extensions(None, user_dir=str(home / "extensions"))
        assert result.extensions == []
        reasons = {r.identifier: r.reason for r in result.withheld}
        assert "':'" in reasons["a:b"] and "rename the file" in reasons["a:b"]
        assert "whitespace" in reasons["c d"]

    def test_an_explicit_unusable_path_raises(self, tmp_path):
        bad = tmp_path / "a:b.py"
        bad.write_text(_DROPIN)
        with pytest.raises(ValueError, match="cannot load it"):
            resolve_explicit(str(bad), [])

    def test_a_dash_or_a_dot_in_a_stem_is_fine(self, home):
        (home / "extensions" / "my-ext.py").write_text(_DROPIN)
        (home / "extensions" / "my.ext.py").write_text(_DROPIN)
        idents = [s.identifier for s in discover_dropins(home / "extensions")]
        assert idents == ["my-ext", "my.ext"]


class TestExplicitModuleNames:
    def test_an_entry_point_module_resolves_exactly_and_by_its_last_segment(self, site):
        root, top = site
        module = _install(root, top=top)
        packages = discover_packages()
        assert resolve_explicit(module, packages).identifier == module
        assert resolve_explicit("ext", packages).identifier == module

    def test_an_ambiguous_segment_is_refused_naming_both(self, site, tmp_path, monkeypatch):
        root, top = site
        first = _install(root, top=top)
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.syspath_prepend(str(other))
        second = _install(other, top=top + "_b", dist="tau-fake-b")
        with pytest.raises(ValueError, match=f"{first}.*{second}|{second}.*{first}"):
            resolve_explicit("ext", discover_packages())
        for name in [m for m in sys.modules if m.startswith(top + "_b")]:
            del sys.modules[name]

    def test_a_path_that_is_an_installed_packages_module_is_refused(self, site):
        root, top = site
        module = _install(root, top=top)
        with pytest.raises(
            ValueError, match=f"module {module} of the installed package tau-fake 1.0"
        ):
            resolve_explicit(str(root / top / "ext.py"), discover_packages())
        assert top not in sys.modules

    def test_a_path_into_an_editable_install_is_refused(self, site, tmp_path):
        root, top = site
        src = tmp_path / "checkout"
        (src / "src" / top).mkdir(parents=True)
        (src / "src" / top / "ext.py").write_text(_EXT_SOURCE)
        module = _install(root, top=top, editable=src)
        with pytest.raises(ValueError, match=f"-e {module}"):
            resolve_explicit(str(src / "src" / top / "ext.py"), discover_packages())

    @pytest.mark.parametrize("shape", ["finder", "finder-plain", "pth"])
    def test_an_editable_install_with_a_custom_package_dir_is_refused(self, site, tmp_path, shape):
        root, top = site
        checkout = tmp_path / "checkout"
        package_dir = checkout / "lib" / ("weird" if shape.startswith("finder") else top)
        module = _install_editable(
            root, top=top, checkout=checkout, package_dir=package_dir, shape=shape
        )
        with pytest.raises(ValueError, match=f"-e {module}"):
            resolve_explicit(str(package_dir / "ext.py"), discover_packages())
        assert top not in sys.modules

    @pytest.mark.parametrize(
        "finder",
        [
            "MAPPING: dict[str, str] = dict(x='/nowhere')\n",
            "import sys\n",
            "def broken(:\n",
            "MAPPING = {['a']: 'b'}\n",
        ],
        ids=["not-a-literal", "no-mapping", "unparseable", "unhashable-key"],
    )
    def test_an_unreadable_finder_refuses_a_path_under_its_checkout(self, site, tmp_path, finder):
        root, top = site
        checkout = tmp_path / "checkout"
        package_dir = checkout / "lib" / "weird"
        module = _install_editable(
            root, top=top, checkout=checkout, package_dir=package_dir, shape="finder"
        )
        (root / "__editable___tau_fake_ed_1_0_finder.py").write_text(finder)
        with pytest.raises(ValueError, match=f"can't tell whether.*Name the module.*-e {module}"):
            resolve_explicit(str(package_dir / "ext.py"), discover_packages())

    def test_an_unreadable_finder_does_not_block_a_path_elsewhere(self, site, tmp_path):
        root, top = site
        checkout = tmp_path / "checkout"
        _install_editable(
            root, top=top, checkout=checkout, package_dir=checkout / "lib" / "x", shape="finder"
        )
        (root / "__editable___tau_fake_ed_1_0_finder.py").write_text("import sys\n")
        other = tmp_path / "elsewhere" / "mine.py"
        other.parent.mkdir()
        other.write_text(_DROPIN)
        assert resolve_explicit(str(other), discover_packages()).kind == "path"

    def test_a_pth_of_only_an_import_hook_is_unreadable_not_guessed(self, site, tmp_path):
        """No finder and no path entry: the root/src probe would be a guess, so it refuses."""
        root, top = site
        checkout = tmp_path / "checkout"
        package_dir = checkout / "src" / top
        _install_editable(root, top=top, checkout=checkout, package_dir=package_dir, shape="pth")
        (root / "__editable__.tau_fake_ed-1.0.pth").write_text("import some_redirector\n")
        with pytest.raises(
            ValueError, match=f"can't tell whether this is the module {top}.ext .*names no source"
        ):
            resolve_explicit(str(package_dir / "ext.py"), discover_packages())

    @pytest.mark.parametrize(
        "rel", ["other.py", "examples/scratch_ext.py", "ext/helper.py", "notext/__init__.py"]
    )
    def test_a_file_that_cannot_be_the_module_is_a_path_even_unreadable(self, site, tmp_path, rel):
        """Only a ``<last segment>.py`` or ``<last segment>/__init__.py`` can be ``top.ext``."""
        root, top = site
        checkout = tmp_path / "checkout"
        _install_editable(
            root, top=top, checkout=checkout, package_dir=checkout / "src" / top, shape="pth"
        )
        (root / "__editable__.tau_fake_ed-1.0.pth").write_text("import some_redirector\n")
        other = checkout / rel
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(_DROPIN)
        assert resolve_explicit(str(other), discover_packages()).kind == "path"

    def test_a_package_dir_named_like_the_module_is_refused_when_unreadable(self, site, tmp_path):
        root, top = site
        checkout = tmp_path / "checkout"
        _install_editable(
            root, top=top, checkout=checkout, package_dir=checkout / "src" / top, shape="pth"
        )
        (root / "__editable__.tau_fake_ed-1.0.pth").write_text("import some_redirector\n")
        pkg = checkout / "elsewhere" / "ext"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(_DROPIN)
        with pytest.raises(ValueError, match=f"module {top}.ext .*-e {top}.ext"):
            resolve_explicit(str(pkg), discover_packages())

    def test_a_pth_that_is_not_text_is_unreadable(self, site, tmp_path):
        root, top = site
        checkout = tmp_path / "checkout"
        package_dir = checkout / "src" / top
        _install_editable(root, top=top, checkout=checkout, package_dir=package_dir, shape="pth")
        (root / "__editable__.tau_fake_ed-1.0.pth").write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(ValueError, match="cannot be read"):
            resolve_explicit(str(package_dir / "ext.py"), discover_packages())

    @pytest.mark.parametrize("mode", ["lenient", "strict"])
    def test_a_real_setuptools_editable_build_is_read(self, site, tmp_path, mode):
        """A real PEP 660 editable wheel from the installed setuptools, unpacked as pip would."""
        _require_setuptools()
        root, top = site
        checkout = tmp_path / "checkout"
        package_dir = checkout / "lib" / "weirddir"
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("")
        (package_dir / "ext.py").write_text(_DROPIN)
        (checkout / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools>=64"]\n'
            'build-backend = "setuptools.build_meta"\n'
            f'[project]\nname = "{top}"\nversion = "1.0"\n'
            f'[project.entry-points."{ENTRY_POINT_GROUP}"]\next = "{top}.ext"\n'
            f'[tool.setuptools]\npackage-dir = {{"{top}" = "lib/weirddir"}}\n'
            f'packages = ["{top}"]\n'
        )
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        script = (
            "import os, sys, setuptools.build_meta as b; os.chdir(sys.argv[1]); "
            "print(b.build_editable(sys.argv[2], {'editable_mode': sys.argv[3]}))"
        )
        built = subprocess.run(
            [sys.executable, "-c", script, str(checkout), str(wheels), mode],
            capture_output=True,
            text=True,
            check=True,
        )
        with zipfile.ZipFile(wheels / built.stdout.strip().splitlines()[-1]) as wheel:
            wheel.extractall(root)
        [info] = root.glob(f"{top}-1.0.dist-info")
        (info / "direct_url.json").write_text(
            json.dumps({"url": f"file://{checkout}", "dir_info": {"editable": True}})
        )
        with pytest.raises(ValueError, match=f"-e {top}.ext"):
            resolve_explicit(str(package_dir / "ext.py"), discover_packages())

    def test_a_mapped_editable_install_is_not_probed_at_its_root(self, site, tmp_path):
        """With a mapping, the root/src guess is not consulted: a stray copy there is a path."""
        root, top = site
        checkout = tmp_path / "checkout"
        _install_editable(
            root, top=top, checkout=checkout, package_dir=checkout / "lib" / "x", shape="finder"
        )
        stray = checkout / "src" / top
        stray.mkdir(parents=True)
        (stray / "ext.py").write_text(_DROPIN)
        assert resolve_explicit(str(stray / "ext.py"), discover_packages()).kind == "path"

    @pytest.mark.parametrize(
        "spec",
        [
            "{dir}/ext.pyc",
            "{dir}/__pycache__/ext.cpython-311.pyc",
            "{dir}/ext.pyo",
            "{top}.ext.pyc",
        ],
    )
    def test_a_bytecode_file_is_refused_before_anything_resolves(self, site, spec):
        """A bare ``pkg.ext.pyc`` would otherwise reach find_spec, which imports ``pkg.ext``."""
        root, top = site
        _install(root, top=top)
        raw = spec.format(dir=root / top, top=top)
        with pytest.raises(ValueError, match="compiled bytecode.*load the .py source or name"):
            resolve_explicit(raw, discover_packages())
        assert top not in sys.modules

    def test_a_path_beside_a_package_is_not_mistaken_for_it(self, site, tmp_path):
        root, top = site
        _install(root, top=top)
        other = tmp_path / "ext.py"
        other.write_text(_DROPIN)
        assert resolve_explicit(str(other), discover_packages()).kind == "path"

    def test_an_importable_module_without_an_entry_point_loads_by_name(self):
        assert resolve_explicit("json", []).kind == "module"

    def test_a_name_nothing_provides_says_how_to_name_a_path(self):
        with pytest.raises(ValueError, match="needs a '/'"):
            resolve_explicit("surely_no_such_module_here", [])

    async def test_an_explicit_module_needs_no_record(self, site, home):
        root, top = site
        module = _install(root, top=top)
        AuthorizationStore.load(home / STORE_FILENAME).save()
        result = await _load_extensions([module], user_dir=str(home / "extensions"))
        assert [e.path for e in result.extensions] == [module]
        assert result.withheld == []


class TestCollisions:
    async def test_a_drop_in_and_a_package_with_one_identifier_load_once(self, site, home):
        root, top = site
        module = _install(root, top=top)
        pkg_dir = home / "extensions" / module
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text(_DROPIN)
        _authorize_all(home)
        result = await _load_extensions(None, user_dir=str(home / "extensions"))
        assert [e.path for e in result.extensions] == [str(pkg_dir)]
        [record] = result.withheld
        assert (record.kind, record.state) == ("package", "collision")

    async def test_an_explicit_name_claims_its_identifier_over_a_discovered_one(
        self, home, tmp_path
    ):
        (home / "extensions" / "gate.py").write_text(_DROPIN)
        mine = tmp_path / "gate.py"
        mine.write_text(_DROPIN)
        result = await _load_extensions([str(mine)], user_dir=str(home / "extensions"))
        assert [e.path for e in result.extensions] == [str(mine)]
        assert [(r.kind, r.state) for r in result.withheld] == [("dropin", "collision")]

    async def test_one_file_named_twice_loads_once(self, home):
        path = home / "extensions" / "twice.py"
        path.write_text(_DROPIN)
        result = await _load_extensions([str(path)], user_dir=str(home / "extensions"))
        assert len(result.extensions) == 1 and result.extensions[0].explicit


class TestTheListingAndTheCli:
    def test_a_listing_without_a_session_writes_nothing(self, home):
        (home / "extensions" / "old.py").write_text(_DROPIN)
        rows = extension_records(extensions_dir=home / "extensions")
        assert [(r.identifier, r.state) for r in rows] == [("old", "authorized")]
        assert not (home / STORE_FILENAME).exists()

    def test_authorize_then_revoke_round_trips(self, site, home):
        root, top = site
        module = _install(root, top=top)
        AuthorizationStore.load(home / STORE_FILENAME).save()
        assert (
            authorize_discovered_extension("ext", extensions_dir=home / "extensions").identifier
            == module
        )
        rows = {
            r.identifier: r.state for r in extension_records(extensions_dir=home / "extensions")
        }
        assert rows[module] == "authorized"
        assert revoke_extension_authorization(module, extensions_dir=home / "extensions") == 1
        rows = {
            r.identifier: r.state for r in extension_records(extensions_dir=home / "extensions")
        }
        assert rows[module] == "unauthorized"

    async def test_a_session_authorizes_and_loads_a_withheld_extension(self, site, home):
        root, top = site
        module = _install(root, top=top)
        AuthorizationStore.load(home / STORE_FILENAME).save()
        session = _session()
        await session.load_extensions(None, user_dir=str(home / "extensions"))
        assert [r.state for r in session.extension_records()] == ["unauthorized"]

        outcome = await session.authorize_extension("ext")
        assert outcome.ok, outcome.message
        assert [(r.identifier, r.state) for r in session.extension_records()] == [
            (module, "loaded")
        ]
        assert sys.modules[module].STARTS == ["enable"]
        assert (
            AuthorizationStore.load(home / STORE_FILENAME)
            .verdict(next(s for s in discover_packages() if s.identifier == module))
            .verdict
            == "authorized"
        )
