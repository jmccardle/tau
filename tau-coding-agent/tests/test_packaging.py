"""What ships has to be what runs.

Two ways to get τ out of this tree — ``pip install`` a package, or unpack the
``package.sh`` tarball — and both once dropped the same class of file: the
non-``.py`` data the installed code reads at runtime. That failure installs
cleanly and dies on first use, which is worse than a build error.

So the two manifests are held against the tree: every non-``.py`` file under a
``src/`` tree must be declared in its package's ``package-data``, and the
tarball script must copy its suffix.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGES = ("tau-llm", "tau-agent-core", "tau-coding-agent")


def _runtime_data_files() -> list[Path]:
    """Every non-.py file under a shipped src/ tree."""
    found: list[Path] = []
    for pkg in PACKAGES:
        src = REPO / pkg / "src"
        found += [
            p
            for p in src.rglob("*")
            if p.is_file()
            and p.suffix != ".py"
            and "__pycache__" not in p.parts
            # ``*.egg-info/`` is build metadata setuptools writes into src/ on any
            # editable install -- the very install CLAUDE.md tells you to do. It is
            # generated, gitignored, and never shipped, so holding it against
            # package-data asserts something this test does not mean. It went
            # unnoticed because .gitignore hides it: the tree looks clean, and the
            # failure only appears after `pip install -e`.
            and not any(part.endswith(".egg-info") for part in p.parts)
        ]
    return found


def test_every_runtime_data_file_is_declared_as_package_data():
    missing: list[str] = []
    for path in _runtime_data_files():
        pkg_dir = path.parents[0]
        pkg_root = path.relative_to(REPO).parts[0]
        pyproject = tomllib.loads((REPO / pkg_root / "pyproject.toml").read_text())
        declared = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {})
        names = declared.get(pkg_dir.name, [])
        if path.name not in names:
            missing.append(f"{path.relative_to(REPO)} (not in {pkg_root} package-data)")
    assert not missing, "runtime data files a wheel would not contain:\n" + "\n".join(missing)


def test_the_tarball_script_copies_every_runtime_suffix():
    script = (REPO / "package.sh").read_text()
    patterns = re.findall(r'-name "\*(\.[A-Za-z0-9]+)"', script)
    missing = sorted({p.suffix for p in _runtime_data_files() if p.suffix not in patterns})
    assert not missing, (
        f"package.sh copies {sorted(set(patterns))} but the tree also ships "
        f"{missing} — the tarball would install and then fail at runtime."
    )


@pytest.mark.parametrize("name", ["tau_default_config.json", "parley.tcss"])
def test_the_two_load_bearing_data_files_are_where_the_code_looks(name):
    """Named explicitly, because each has its own failure mode: a missing
    config template kills first run, a missing stylesheet kills the TUI."""
    assert (REPO / "tau-coding-agent/src/tau_coding_agent" / name).is_file()


# -- declared dependencies vs actual imports --------------------------------

#: Third-party top-level modules each package's shipped code imports, and the
#: distribution that provides them. Kept explicit: the point is to catch a NEW
#: import that nobody declared, not to re-derive the mapping every run.
PROVIDES = {
    "httpx": "httpx",
    "pydantic": "pydantic",
    "nats": "nats-py",
    "textual": "textual",
    "typer": "typer",
    "pytest": "pytest",  # tau_agent_core.testing, via the [testing] extra
}


def _third_party_imports(pkg: str) -> set[str]:
    import ast
    import sys

    stdlib = set(sys.stdlib_module_names)
    local = {"tau_llm", "tau_agent_core", "tau_coding_agent", "tau_jmfts"}
    found: set[str] = set()
    for path in (REPO / pkg / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top and top not in stdlib and top not in local:
                    found.add(top)
    return found


@pytest.mark.parametrize("pkg", [*PACKAGES, "tau-jmfts"])
def test_every_imported_third_party_module_is_declared(pkg):
    """A module imported at package scope but absent from `dependencies` is an
    install that succeeds and then fails on `import`. τ shipped exactly that
    (httpx, undeclared by tau-llm) until a clean container caught it."""
    pyproject = tomllib.loads((REPO / pkg / "pyproject.toml").read_text())
    declared = " ".join(pyproject["project"].get("dependencies", []))
    for extra in pyproject["project"].get("optional-dependencies", {}).values():
        declared += " " + " ".join(extra)

    missing = []
    for mod in sorted(_third_party_imports(pkg)):
        dist = PROVIDES.get(mod, mod)
        if dist not in declared:
            missing.append(f"{mod} (provides: {dist})")
    assert not missing, (
        f"{pkg} imports modules it does not declare: {missing}. Add them to "
        f"[project] dependencies, or to an optional extra if the import is opt-in."
    )


# -- version, and the one place it is written ------------------------------

#: Every distribution in this tree, and the module whose ``__version__`` is its
#: version. They release in lockstep: one ``--version`` number, four wheels.
DISTRIBUTIONS = {
    "tau-llm": "tau_llm",
    "tau-agent-core": "tau_agent_core",
    "tau-coding-agent": "tau_coding_agent",
    "tau-jmfts": "tau_jmfts",
}


def _declared_version(module: str) -> str:
    """Read ``__version__`` out of a package's ``__init__.py`` WITHOUT importing it.

    This is the same static read setuptools performs for ``[tool.setuptools.dynamic]
    version = {attr = ...}``, so the value here is the value that lands in the wheel.
    Importing instead would prove less: it would resolve through whatever is
    installed in this environment rather than through the source tree.
    """
    import ast

    pkg = next(p for p, m in DISTRIBUTIONS.items() if m == module)
    tree = ast.parse((REPO / pkg / "src" / module / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant), f"{module}.__version__ is not a literal"
            return str(node.value.value)
    raise AssertionError(f"{module}/__init__.py declares no __version__")


@pytest.mark.parametrize("pkg,module", sorted(DISTRIBUTIONS.items()))
def test_each_distribution_takes_its_version_from_its_package(pkg, module):
    """No ``version = "..."`` literal in a pyproject.

    A literal is a second copy of the number, and the copies drift: this tree
    shipped ``version = "0.0.0"`` in all four pyprojects while ``tau --version``
    said ``0.9.1``. Declaring it dynamic makes the wheel metadata and the running
    code the same line of source.
    """
    pyproject = tomllib.loads((REPO / pkg / "pyproject.toml").read_text())
    project = pyproject["project"]
    assert "version" not in project, (
        f"{pkg}/pyproject.toml pins version={project.get('version')!r} as a literal; "
        f'use dynamic = ["version"] reading {module}.__version__ instead.'
    )
    assert "version" in project.get("dynamic", [])
    attr = pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == f"{module}.__version__", attr


def test_all_four_distributions_carry_the_same_version():
    """They are built from one tree, tested only against each other, and installed
    together by every extra in this repo. A mixed set is a combination nothing here
    has run, so the numbers must agree."""
    versions = {module: _declared_version(module) for module in DISTRIBUTIONS.values()}
    assert len(set(versions.values())) == 1, f"versions disagree: {versions}"


def test_the_repo_root_version_matches():
    """The root pyproject is config, not a distribution (it has no ``[build-system]``
    and is never uploaded), so its version is a plain literal with no package to read
    from. It is still the number a reader sees first."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert root["project"]["version"] == _declared_version("tau_coding_agent")


def _all_requirements(pkg: str) -> list[str]:
    """Every requirement string a distribution declares: base plus every extra."""
    project = tomllib.loads((REPO / pkg / "pyproject.toml").read_text())["project"]
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs += extra
    return reqs


@pytest.mark.parametrize("pkg", sorted(DISTRIBUTIONS))
def test_in_repo_requirements_are_pinned_to_the_lockstep_version(pkg):
    """``==``, not ``>=``. These four are built from one tree and tested only
    against each other, so any other combination is untested -- a later
    ``ffwf-tau-agent-core`` must not resolve against this coding-agent.

    The pin is a literal, which is the one copy of the version number this repo
    cannot make dynamic (a requirement string has nowhere to read from). That is
    exactly why this test exists: it is what keeps the pin from surviving a
    version bump that moved on without it.
    """
    version = _declared_version("tau_coding_agent")
    wrong = [
        r
        for r in _all_requirements(pkg)
        if r.startswith("ffwf-tau") and not r.endswith(f"=={version}")
    ]
    assert not wrong, (
        f"{pkg} declares in-repo requirements that are not pinned to {version}: {wrong}"
    )


@pytest.mark.parametrize("pkg", sorted(DISTRIBUTIONS))
def test_every_distribution_ships_the_licence(pkg):
    """A wheel is built from ONE package directory and cannot reach the repo root,
    so ``license-files = ["LICENSE"]`` needs a copy inside each package. The copies
    are byte-identical to the root by rule; a diverging one is a licensing claim
    nobody made on purpose."""
    project = tomllib.loads((REPO / pkg / "pyproject.toml").read_text())["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "Fight Fire with Fire Robotics, LLC"}]

    shipped = (REPO / pkg / "LICENSE").read_text()
    assert shipped == (REPO / "LICENSE").read_text(), f"{pkg}/LICENSE differs from the root LICENSE"
    assert "Fight Fire with Fire Robotics, LLC" in shipped


def test_the_tarball_stages_every_file_the_metadata_points_at():
    """``package.sh`` stages each package directory by hand, file by file. A
    pyproject that names a file setuptools must read at BUILD time -- the licence
    text, and the readme once one is declared -- turns a forgotten ``cp`` into an
    unpacked tarball that cannot be installed at all. Same class as the missing
    runtime data file above, one step earlier."""
    script = (REPO / "package.sh").read_text()
    for pkg in PACKAGES:  # tau-jmfts is not in the tarball
        project = tomllib.loads((REPO / pkg / "pyproject.toml").read_text())["project"]
        referenced = list(project.get("license-files", []))
        if isinstance(project.get("readme"), str):
            referenced.append(project["readme"])
        for name in referenced:
            assert f'cp "$pkg/{name}"' in script, (
                f"{pkg}/pyproject.toml references {name}, but package.sh never copies "
                f"it into the staged package directory."
            )


#: The private git remote. `github` is the public release mirror with truncated
#: history; `origin` points at a private host carrying unfiltered history. The two
#: are one `git remote -v` away from each other, which is exactly how a private
#: hostname ends up pasted into a file that gets uploaded to PyPI.
PRIVATE_HOST = "dev.ffwf.net"
PUBLIC_REPO = "https://github.com/jmccardle/tau"


@pytest.mark.parametrize("pkg", sorted(DISTRIBUTIONS))
def test_published_metadata_points_at_the_public_repository(pkg):
    urls = tomllib.loads((REPO / pkg / "pyproject.toml").read_text())["project"]["urls"]
    assert urls["Repository"] == PUBLIC_REPO, urls


def test_nothing_published_names_the_private_remote():
    """Everything here is uploaded verbatim: the pyprojects become wheel metadata
    and the READMEs become the PyPI project page. A private hostname in any of
    them is published the moment the release is."""
    published = [REPO / "README.md"]
    for pkg in DISTRIBUTIONS:
        published += [REPO / pkg / "pyproject.toml", REPO / pkg / "README.md"]
    leaked = [p.relative_to(REPO) for p in published if PRIVATE_HOST in p.read_text()]
    assert not leaked, f"{PRIVATE_HOST} appears in files that get published: {leaked}"


# -- the supported-interpreter claim, and the matrix that backs it ----------


def _requires_python_floor(pyproject: Path) -> str:
    """The X.Y in a ``requires-python = ">=X.Y"``. Only ``>=`` is accepted: this
    tree makes one claim about interpreters and a compound specifier would be a
    second, unmeasured one."""
    spec = tomllib.loads(pyproject.read_text())["project"]["requires-python"]
    match = re.fullmatch(r">=(\d+\.\d+)", spec.strip())
    assert match, f"{pyproject.name} declares requires-python={spec!r}; expected a plain >=X.Y"
    return match.group(1)


def _ci_python_matrix() -> list[str]:
    """The interpreters publish.yml's ``test`` job actually runs.

    Read with a regex rather than a YAML parser because the alternative is a
    PyYAML dependency for one assertion, and the matrix is deliberately a single
    flow-style line so that adding a version is a one-token diff. Same trade the
    ``package.sh`` tests above make.
    """
    workflow = (REPO / ".github/workflows/publish.yml").read_text()
    match = re.search(r"^\s*python-version:\s*\[([^\]]*)\]", workflow, re.M)
    assert match, "publish.yml has no inline python-version matrix"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_four_packages_claim_one_python_floor():
    """``requires-python`` is what pip enforces at install time, and the four are
    installed together by every extra here. A package that quietly raised its
    floor would resolve out of an environment the other three accepted."""
    floors = {
        pkg: _requires_python_floor(REPO / pkg / "pyproject.toml") for pkg in DISTRIBUTIONS
    }
    floors["<root>"] = _requires_python_floor(REPO / "pyproject.toml")
    assert len(set(floors.values())) == 1, f"requires-python floors disagree: {floors}"


def test_ci_runs_the_oldest_python_the_packages_claim_to_support():
    """``requires-python = ">=3.11"`` is a promise made to everyone who runs
    ``pip install``; the CI matrix is the only evidence behind it. If the floor
    moves and the matrix does not, the release ships a claim nobody has run --
    which is the same defect as listing a matrix entry nobody has run.

    The converse is not asserted: the matrix may name versions ABOVE the floor,
    and each one there means someone ran the suite on it.
    """
    floor = _requires_python_floor(REPO / "tau-coding-agent/pyproject.toml")
    matrix = _ci_python_matrix()
    assert floor in matrix, (
        f"publish.yml tests {matrix} but the packages claim to support {floor} and up. "
        f"Either test {floor}, or raise requires-python to what is actually tested."
    )
    below = [v for v in matrix if tuple(map(int, v.split("."))) < tuple(map(int, floor.split(".")))]
    assert not below, f"publish.yml tests {below}, which requires-python={floor} excludes"


def test_the_release_pipeline_never_downgrades_a_failure():
    """Fail Early (CLAUDE.md), asserted rather than only commented. Each of
    these turns a broken release into a green run: two would let a failing step
    pass, and ``skip-existing`` would make re-uploading an already-published
    version a silent no-op instead of the error that catches a botched bump.

    Comment lines are dropped first, and not as a convenience: the file's own
    commentary *names* these tokens to explain why they are absent, so scanning
    the raw text would fail on the documentation of the rule it enforces.
    """
    lines = (REPO / ".github/workflows/publish.yml").read_text().splitlines()
    code = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    found = [t for t in ("continue-on-error", "|| true", "skip-existing:") if t in code]
    assert not found, (
        f"publish.yml contains {found}. The release pipeline must fail loudly; "
        f"see the header comment and CLAUDE.md."
    )


def test_the_openai_sdk_is_not_a_dependency():
    """τ speaks the /chat/completions wire format with httpx. The `openai`
    package was declared and never imported; this keeps it gone."""
    pyproject = tomllib.loads((REPO / "tau-llm/pyproject.toml").read_text())
    deps = pyproject["project"]["dependencies"]
    assert not any(d.split(">")[0].split("=")[0].strip() == "openai" for d in deps), deps
