# Phase 0 Subphases — Monorepo Setup

> **Duration**: 1 day
> **Goal**: A working monorepo with all 3 packages importable and testable.

## Subphase 0.1 — Workspace and Virtual Environment

**Topic**: Set up the root workspace, Python version, and virtual environment.

### Done Criteria

- Python 3.11+ available on PATH (confirmed with `python --version`)
- `venv/` directory created with active packages: `pytest`, `ruff`, `mypy`, `httpx-mock`, `pytest-asyncio`
- Root `pyproject.toml` declares workspace members (if using uv/poetry, or manual path setup)

### Reference

- `MONOREPO-STRUCTURE.md` lines 1-10: monorepo overview
- `MONOREPO-STRUCTURE.md` lines 20-30: package dependency graph

### Testing

```bash
# 1. Verify Python version
python --version  # should be 3.11+

# 2. Verify packages
pip list | grep -E 'pytest|ruff|mypy|httpx-mock|pytest-asyncio'

# 3. Verify workspace resolves
python -c "import sys; print(sys.path)"  # should include venv/...
```

### Success Signal

An agent running any `pytest --co` from the root directory can discover test collection hooks for all 3 packages (even if tests don't exist yet). The `venv/` is recognized by IDEs and tools.

---

## Subphase 0.2 — Package Scaffolding

**Topic**: Create `pyproject.toml`, `src/` layout, `__init__.py`, and test directories for all 3 packages.

### Done Criteria

Each package has:

- `pyproject.toml` with `[project]` table (name, version, description, dependencies)
- `src/<package_name>/` directory with `__init__.py`
- `tests/` directory with `__init__.py` and `conftest.py`
- `__init__.py` exports are stubs (not yet implemented)

Package `pyproject.toml` files:

| Package | Name in pyproject.toml | Dependencies |
|---------|----------------------|--------------|
| tau-ai | `tau-ai` | `pydantic>=2.0`, `openai>=1.0` |
| tau-agent-core | `tau-agent-core` | `tau-ai`, `pydantic>=2.0` |
| tau-coding-agent | `tau-coding-agent` | `tau-agent-core`, `textual>=0.47`, `typer` |

### Reference

- `MONOREPO-STRUCTURE.md` lines 11-50: full directory layout
- `MONOREPO-STRUCTURE.md` lines 52-70: package dependencies

### Testing

```bash
# 1. Install packages in editable mode
cd ~/Development/agent-harness-py
pip install -e ./tau-ai -e ./tau-agent-core -e ./tau-coding-agent

# 2. Verify imports resolve (all will fail with ImportError — that's expected)
python -c "from tau_ai.types import UserMessage"
# Expected: ImportError: cannot import name 'UserMessage'

python -c "from tau_agent_core import AgentSession"
# Expected: ImportError: cannot import name 'AgentSession'

python -c "from tau_coding_agent import App"
# Expected: ImportError: cannot import name 'App'

# 3. Verify dependency chain
pip show tau-agent-core | grep -A1 Depends
# Should show: Depends: tau-ai

# 4. Run pytest (should find no tests, but should not error)
pytest --co -q
# Expected: 0 tests collected
```

### Success Signal

All 3 packages are importable (even if they contain only stubs), and the dependency chain `tau-coding-agent → tau-agent-core → tau-ai` is correctly resolved by pip. `pytest --co` discovers the test directories.

---

## Subphase 0.3 — Cross-Phase Type Skeleton

**Topic**: Create the type skeleton files that Phase 1 will fill in. These are the types defined in `SUBPHASE-0.0.md` but with `...` bodies.

### Done Criteria

The following files exist with the exact type signatures from `SUBPHASE-0.0.md`, but with `...` (Ellipsis) bodies:

```
tau-ai/src/tau_ai/types.py          # UserMessage, AssistantMessage, ToolResultMessage, etc.
tau-ai/src/tau_ai/tools.py          # ToolDefinition, define_tool stub
tau-ai/src/tau_ai/abort.py          # AbortSignal stub
tau-agent-core/src/tau_agent_core/events.py  # AgentEvent stub
tau-agent-core/src/tau_agent_core/session.py # SessionEntry types stub
tau-agent-core/src/tau_agent_core/extension_types.py  # ExtensionAPI stub
```

Each file has docstrings referencing the corresponding section in `SUBPHASE-0.0.md`.

### Reference

- `SUBPHASE-0.0.md` lines 120-260: core data type contracts
- `MONOREPO-STRUCTURE.md` lines 11-50: directory layout

### Testing

```bash
# 1. All types should import without error (because bodies are ...)
python -c "from tau_ai.types import UserMessage, AssistantMessage, ToolResultMessage"
python -c "from tau_ai.tools import define_tool"
python -c "from tau_ai.abort import AbortSignal"
python -c "from tau_agent_core.events import AgentEvent"
python -c "from tau_agent_core.session import SessionEntry"
python -c "from tau_agent_core.extension_types import ExtensionAPI"

# 2. Type annotations should be valid (checked with mypy)
mypy --no-error-summary tau-ai/src/tau_ai/types.py
mypy --no-error-summary tau-agent-core/src/tau_agent_core/events.py

# 3. Stub types should raise NotImplementedError when called
python -c "from tau_ai.tools import define_tool; define_tool({})"
# Expected: NotImplementedError: Not implemented (subphase 0.3)
```

### Success Signal

All types import successfully. `mypy` reports no type errors. The type signatures exactly match the contracts in `SUBPHASE-0.0.md`. This means Phase 1 can start implementing the types, and the agent loop (Phase 2) can start importing them.

---

## Subphase 0.4 — Testing Infrastructure

**Topic**: Set up test fixtures, conftest.py files, and shared test utilities.

### Done Criteria

- `tau-ai/tests/conftest.py`:
  - `mock_openai_client` fixture (using `openai` mock or `aioresponses`)
  - `sample_messages` fixture (list of UserMessage/AssistantMessage/ToolResultMessage)
  - `sample_tool_call` fixture (a ToolCall with valid arguments)

- `tau-agent-core/tests/conftest.py`:
  - `in_memory_session_manager` fixture
  - `sample_agent_event` fixture
  - `sample_tool_definition` fixture

- `tau-coding-agent/tests/conftest.py`:
  - `mock_agent_session` fixture (pytest-asyncio compatible)
  - `mock_extension_api` fixture

- All packages have a `tests/__init__.py` and a root `pytest.ini` or `pyproject.toml[tool.pytest]` section

### Reference

- `MONOREPO-STRUCTURE.md` lines 20-30: package structure
- `SUBPHASE-0.0.md` lines 260-340: data types for fixtures

### Testing

```bash
# 1. Run pytest in each package (should find fixture imports but no tests)
cd tau-ai && pytest --co -q
cd ../tau-agent-core && pytest --co -q
cd ../tau-coding-agent && pytest --co -q

# 2. Verify fixtures are discoverable
python -c "
import sys
sys.path.insert(0, 'tau-ai/tests')
from conftest import sample_messages, sample_tool_call
print('tau-ai fixtures OK')
"

# 3. Run a quick sanity test
pytest -xvs -k "test_import" --tb=short
```

### Success Signal

All 3 packages can run `pytest --co` and discover the fixtures. The shared test utilities work across packages (e.g., `sample_messages` from tau-ai can be imported into tau-agent-core tests).
