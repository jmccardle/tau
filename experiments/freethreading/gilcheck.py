"""Import each dependency in a fresh subprocess; report GIL state + warnings."""
import subprocess
import sys

MODS = [
    "pydantic", "pydantic_core", "httpx", "anyio", "h11", "jiter", "anthropic",
    "google.genai", "rich", "textual", "prompt_toolkit", "websockets", "nats",
    "PIL.Image", "msgpack", "aiohttp", "yarl", "multidict", "frozenlist",
    "propcache", "markupsafe", "charset_normalizer", "cryptography", "cffi",
    "wcwidth", "tokenizers", "pytest", "pytest_asyncio", "syrupy", "cairosvg",
    "textual_dev", "mypy", "tau_llm", "tau_agent_core", "tau_coding_agent",
    "tau_coding_agent.app", "tau_coding_agent.cli", "tau_coding_agent.serve",
    "tau_jmfts", "tau_meta",
]

CODE = r"""
import sys, warnings
warnings.simplefilter("always")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    import importlib
    importlib.import_module(%r)
gw = [str(x.message) for x in w if "GIL" in str(x.message)]
print(sys._is_gil_enabled(), gw)
"""

for m in MODS:
    r = subprocess.run([sys.executable, "-c", CODE % m], capture_output=True, text=True)
    out = (r.stdout.strip() or r.stderr.strip().splitlines()[-1] if (r.stdout.strip() or r.stderr.strip()) else "")
    err_gil = [ln for ln in r.stderr.splitlines() if "GIL" in ln]
    print(f"{m:28s} rc={r.returncode} {out} {err_gil}")
