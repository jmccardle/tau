"""The base framework never turns the GIL back on (experiments/freethreading/RESULTS.md).

The standalone builds ship free-threaded. A C extension built without
``Py_mod_gil`` silently re-enables the GIL for the whole process the moment it is
imported, and nothing else would notice: every test still passes, just serially.
This is the gate for that. It imports every module of the five τ distributions
and the third-party set they pull in, in a FRESH interpreter with ``PYTHON_GIL``
unset (``PYTHON_GIL=0`` would pin the GIL off and hide the regression), and
names the first import that enabled it.

Extensions are out of scope: an extension may bring the GIL back; τ may not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"),
    reason=(
        "GIL build (Py_GIL_DISABLED is unset): there is no GIL to re-enable. "
        "This gate runs on free-threaded interpreters (3.14t, 3.15t)."
    ),
)

REPO = Path(__file__).resolve().parents[2]
DISTRIBUTIONS = ("tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta")
PACKAGES = ("tau_llm", "tau_agent_core", "tau_coding_agent", "tau_jmfts", "tau_meta")

THIRD_PARTY = (
    "pydantic",
    "pydantic_core",
    "httpx",
    "anyio",
    "jiter",
    "anthropic",
    "google.genai",
    "rich",
    "textual",
    "prompt_toolkit",
    "websockets",
    "nats",
    "PIL.Image",
)

_PROBE = r"""
import importlib, json, pkgutil, sys, warnings
packages, third_party = json.loads(sys.argv[1])
tau = []
for pkg in packages:
    mod = importlib.import_module(pkg)
    tau.append(pkg)
    for info in pkgutil.walk_packages(mod.__path__, pkg + "."):
        tau.append(info.name)
names = list(third_party) + [n for n in tau if not n.endswith(".__main__")]
culprit = None
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    for name in names:
        importlib.import_module(name)
        if culprit is None and sys._is_gil_enabled():
            culprit = name
print(json.dumps({
    "tau_modules": sorted(tau),
    "gil_enabled": sys._is_gil_enabled(),
    "culprit": culprit,
    "warnings": [str(w.message) for w in caught if "GIL" in str(w.message)],
}))
"""


def _modules_on_disk() -> list[str]:
    """Every τ module, named from the five ``src`` trees, so the walk cannot quietly shrink."""
    names = []
    for dist in DISTRIBUTIONS:
        src = REPO / dist / "src"
        for path in src.rglob("*.py"):
            parts = path.relative_to(src).with_suffix("").parts
            if any(part.endswith(".egg-info") for part in parts):
                continue
            names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return sorted(names)


def test_importing_tau_and_its_dependencies_leaves_the_gil_disabled() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps([PACKAGES, THIRD_PARTY])],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, f"an import failed:\n{proc.stderr[-3000:]}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["tau_modules"] == _modules_on_disk(), "the walk missed or invented a module"
    assert not report["gil_enabled"], (
        f"importing {report['culprit']!r} re-enabled the GIL; warnings: {report['warnings']}"
    )
