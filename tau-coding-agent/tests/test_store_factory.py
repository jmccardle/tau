"""Tests for ``tau_coding_agent.store_factory``: resolving ``--store`` / config
``session_store`` into a :class:`SessionCatalog` (W12).

Reference: docs/JMFTS-INTEGRATION-PLAN.md §3.1. The unreachable-server case uses
``http://127.0.0.1:1`` (port 1 -- always connection-refused, never a real
server), the same trick ``tau-jmfts/tests/test_client_transport.py`` uses, so
this test needs no live JMFTS instance and fails fast rather than timing out.
A live end-to-end round trip (build a working JmftsSessionCatalog against a
real server) is exercised manually / in tau-jmfts's own ``-m jmfts`` suite, not
duplicated here.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.session_store import FileSessionCatalog
from tau_coding_agent.store_factory import (
    StoreError,
    build_jmfts_client,
    build_session_catalog,
    resolve_backend_name,
    resolve_host_parent_id,
)

UNREACHABLE_URL = "http://127.0.0.1:1"


# ── resolve_backend_name: the §3.1 resolution order ─────────────────────────


def test_store_flag_wins_over_config():
    config = {"session_store": {"backend": "jmfts"}}
    assert resolve_backend_name(config, "file") == "file"


def test_config_backend_used_when_no_flag():
    config = {"session_store": {"backend": "jmfts"}}
    assert resolve_backend_name(config, None) == "jmfts"


def test_defaults_to_file_when_neither_set():
    assert resolve_backend_name({}, None) == "file"


def test_session_store_not_an_object_raises():
    with pytest.raises(StoreError, match="session_store"):
        resolve_backend_name({"session_store": "jmfts"}, None)


def test_backend_not_a_string_raises():
    with pytest.raises(StoreError, match="backend"):
        resolve_backend_name({"session_store": {"backend": 7}}, None)


# ── build_session_catalog: backend dispatch ─────────────────────────────────


def test_file_backend_returns_file_session_catalog():
    catalog = build_session_catalog({}, "file")
    assert isinstance(catalog, FileSessionCatalog)


def test_unknown_backend_raises_not_defaults_to_file():
    with pytest.raises(StoreError, match="unknown session_store backend 'bogus'"):
        build_session_catalog({}, "bogus")


def test_unknown_config_backend_raises():
    with pytest.raises(StoreError, match="unknown session_store backend"):
        build_session_catalog({"session_store": {"backend": "sqlite"}}, None)


# ── the jmfts branch: config validation before any network call ────────────


def test_jmfts_backend_with_no_url_raises_before_any_network_call():
    with pytest.raises(StoreError, match="no URL is configured"):
        build_session_catalog({"session_store": {"backend": "jmfts"}}, None)


def test_jmfts_url_not_a_string_raises():
    with pytest.raises(StoreError, match="must be a string"):
        build_jmfts_client({"session_store": {"url": 7}})


def test_jmfts_url_from_env_var(monkeypatch):
    monkeypatch.setenv("JMFTS_API_URL", UNREACHABLE_URL)
    # No "url" key in config -> falls back to $JMFTS_API_URL -> reaches the
    # (failing) health check, proving the env var was actually read.
    with pytest.raises(StoreError, match="unreachable"):
        build_jmfts_client({"session_store": {}})


def test_jmfts_unreachable_url_fails_loudly_at_construction_not_first_append():
    # The §3.1 startup health check: a dead server must fail HERE, building the
    # client/catalog, never deferred to the first append.
    with pytest.raises(StoreError, match="unreachable"):
        build_session_catalog({"session_store": {"backend": "jmfts", "url": UNREACHABLE_URL}}, None)


def test_no_session_does_not_contact_an_unreachable_jmfts_store():
    """Reported by Tectum's prototyping: ``--no-session`` in text mode still
    required the JMFTS store, and exited 2 at startup when it was unreachable.

    The run had been told not to write anything. ``create_ephemeral`` is
    in-memory under both stores, so the server is not a dependency of an
    ephemeral run, and refusing to start is refusing over a dependency that is
    not there. The scoping fix is ``persist=False`` skipping the ``GET /``.

    MUTATION TARGET: drop the ``persist``/``health_check`` thread-through so
    ``build_jmfts_client`` always health-checks → red with the same
    ``StoreError`` the test above asserts for the persisted case.
    """
    catalog = build_session_catalog(
        {"session_store": {"backend": "jmfts", "url": UNREACHABLE_URL}}, None, persist=False
    )
    session = catalog.create_ephemeral("/tmp/anywhere", "m", "openai")
    session.append_message({"role": "user", "content": "no server was ever contacted"})
    assert [m["content"] for m in session.messages] == ["no server was ever contacted"]


def test_no_session_still_refuses_a_misconfigured_store():
    """``persist=False`` drops the network CONTACT, not the validation. A
    backend name nobody implements, or a jmfts store with no URL at all, is
    wrong whether or not a server is up, and still raises.

    This is the line that keeps the fix from becoming "--no-session disables
    Fail-Early on the store".
    """
    with pytest.raises(StoreError, match="unknown session_store backend"):
        build_session_catalog({"session_store": {"backend": "nope"}}, None, persist=False)
    with pytest.raises(StoreError, match="no URL is configured"):
        build_session_catalog({"session_store": {"backend": "jmfts"}}, None, persist=False)


def _run_cli(tmp_path, *argv: str, stdin: str | None = None):
    """A real ``tau`` child with its own ``$HOME``, an unreachable model AND an
    unreachable JMFTS store.

    Both are dead on purpose: the question is WHICH one the run dies on. A
    startup that dies on the store never reaches the model, so "it got as far
    as the model" is the assertion that the store was passed over. Driving the
    actual CLI rather than ``run_print`` is deliberate — the defect class here
    is a flag that does not reach behaviour across files, which an in-process
    call with a hand-built ``CLIArgs`` can step over.
    """
    import json
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    (home / ".tau").mkdir(parents=True)
    (home / ".tau" / "config.json").write_text(
        json.dumps(
            {
                "models": {
                    "fake": {
                        "backend": "openai",
                        "model": "fake-model",
                        "base_url": f"{UNREACHABLE_URL}/v1",
                        "api_key": "x",
                        "tools": [],
                    }
                },
                "default_model": "fake",
                "session_store": {"backend": "jmfts", "url": UNREACHABLE_URL},
            }
        )
    )
    return subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", *argv],
        input=stdin,
        cwd=str(tmp_path),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_no_session_run_starts_without_the_store_end_to_end(tmp_path):
    """Tectum's report, as the child process they actually ran.

    The run must get past startup and fail on the MODEL instead — proof that
    the store was never contacted, since the store check happens first and
    would have ended the run before any provider call.
    """
    result = _run_cli(tmp_path, "-p", "--no-session", "hi")

    assert "JMFTS store" not in result.stderr, (
        f"--no-session still contacted the session store: {result.stderr}"
    )
    assert result.returncode != 2, f"refused at startup: {result.stderr}"
    assert "All connection attempts failed" in result.stderr, (
        "expected the run to reach the (unreachable) model; instead it ended "
        f"with: {result.stderr[-2000:]}"
    )


def test_the_same_run_without_the_flag_still_refuses_at_startup(tmp_path):
    """The contrast. Without ``--no-session`` the run WILL append, so an
    unreachable store is a real missing dependency and §3.1's startup health
    check must still end it before a single token is spent.

    Paired with the test above so the fix reads as a scoping of the check
    rather than a removal of it.
    """
    result = _run_cli(tmp_path, "-p", "hi")

    assert result.returncode == 2, result.stderr
    assert "JMFTS store" in result.stderr and "unreachable" in result.stderr


def test_a_no_session_rpc_server_starts_and_serves_without_the_store(tmp_path):
    """The same scoping over the wire, where it means something weaker.

    ``--mode rpc`` keeps ``list_sessions``/``switch_session``/``fork``/
    ``new_session {"persist": true}`` reachable under ``--no-session``, so the
    catalog is still BUILT — only the startup contact is dropped. A host that
    asks none of those four never needs the server up, which is the case this
    pins: the process starts, answers, and exits cleanly on stdin EOF.
    """
    import json

    result = _run_cli(
        tmp_path,
        "--mode",
        "rpc",
        "--no-session",
        stdin=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "get_state"}) + "\n",
    )

    assert result.returncode == 0, f"child failed: {result.stderr}"
    assert "JMFTS store" not in result.stderr, result.stderr
    state = json.loads(result.stdout.splitlines()[0])["result"]
    assert state["addressable"] is False


def test_an_rpc_server_without_the_flag_still_refuses_at_startup(tmp_path):
    """The wire contrast: a persisted RPC run will append, so §3.1's check
    still ends it at startup rather than at the first write."""
    result = _run_cli(tmp_path, "--mode", "rpc", stdin="")

    assert result.returncode == 2, result.stdout
    assert "JMFTS store" in result.stderr and "unreachable" in result.stderr


def test_the_persisted_default_is_unchanged_by_the_new_parameter():
    """``persist`` defaults to True, so every existing caller — the TUI,
    ``--import-session``/``--export-session``, and any persisted run — still
    gets the §3.1 startup health check with no argument passed."""
    with pytest.raises(StoreError, match="unreachable"):
        build_session_catalog({"session_store": {"backend": "jmfts", "url": UNREACHABLE_URL}}, None)
    with pytest.raises(StoreError, match="unreachable"):
        build_jmfts_client({"session_store": {"url": UNREACHABLE_URL}})


def test_jmfts_unreachable_url_closes_the_half_open_client():
    # Regression guard for the "don't leak the httpx.Client" comment in
    # build_jmfts_client: a second call against the same bad URL must behave
    # identically (raise again), not hang or reuse a half-open connection.
    config = {"session_store": {"url": UNREACHABLE_URL}}
    with pytest.raises(StoreError):
        build_jmfts_client(config)
    with pytest.raises(StoreError):
        build_jmfts_client(config)


# ── CR-4: shared-bearer token threading + 401 handling ───────────────────────


def test_build_jmfts_client_threads_config_token_into_bearer_header(monkeypatch):
    # With health() stubbed to succeed, the returned client must carry the
    # config token as an `Authorization: Bearer <token>` header.
    from tau_jmfts.client import JmftsClient

    monkeypatch.setattr(JmftsClient, "health", lambda self: {"status": "ok"})
    client = build_jmfts_client({"session_store": {"url": "http://jmfts.test", "token": "sekret"}})
    try:
        assert client._client.headers["authorization"] == "Bearer sekret"
    finally:
        client.close()


def test_build_jmfts_client_reads_token_from_env(monkeypatch):
    from tau_jmfts.client import JmftsClient

    monkeypatch.setattr(JmftsClient, "health", lambda self: {"status": "ok"})
    monkeypatch.setenv("JMFTS_API_TOKEN", "env-token")
    client = build_jmfts_client({"session_store": {"url": "http://jmfts.test"}})
    try:
        assert client._client.headers["authorization"] == "Bearer env-token"
    finally:
        client.close()


def test_build_jmfts_client_config_token_wins_over_env(monkeypatch):
    from tau_jmfts.client import JmftsClient

    monkeypatch.setattr(JmftsClient, "health", lambda self: {"status": "ok"})
    monkeypatch.setenv("JMFTS_API_TOKEN", "env-token")
    client = build_jmfts_client(
        {"session_store": {"url": "http://jmfts.test", "token": "config-token"}}
    )
    try:
        assert client._client.headers["authorization"] == "Bearer config-token"
    finally:
        client.close()


def test_build_jmfts_client_no_token_sends_no_auth_header(monkeypatch):
    # Fail-Early: no token configured/env means NO Authorization header is
    # fabricated. Against an unauth'd server this is fine; against an auth'd one
    # it 401s loudly (see the next test).
    from tau_jmfts.client import JmftsClient

    monkeypatch.delenv("JMFTS_API_TOKEN", raising=False)
    monkeypatch.setattr(JmftsClient, "health", lambda self: {"status": "ok"})
    client = build_jmfts_client({"session_store": {"url": "http://jmfts.test"}})
    try:
        assert "authorization" not in client._client.headers
    finally:
        client.close()


def test_build_jmfts_client_token_not_a_string_raises(monkeypatch):
    with pytest.raises(StoreError, match="token.*must be a string"):
        build_jmfts_client({"session_store": {"url": "http://jmfts.test", "token": 7}})


def test_build_jmfts_client_401_raises_storeerror_asking_for_token(monkeypatch):
    # An auth'd server rejecting the request must surface as an actionable
    # StoreError telling the user to set a token -- NOT a silent None/allow.
    from tau_jmfts.client import JmftsClient, JmftsError

    def _raise_401(self):
        raise JmftsError(status_code=401, detail="Missing token", url="/", method="GET")

    monkeypatch.delenv("JMFTS_API_TOKEN", raising=False)
    monkeypatch.setattr(JmftsClient, "health", _raise_401)
    with pytest.raises(StoreError, match="token"):
        build_jmfts_client({"session_store": {"url": "http://jmfts.test"}})


def test_build_jmfts_client_401_with_wrong_token_still_raises(monkeypatch):
    from tau_jmfts.client import JmftsClient, JmftsError

    def _raise_401(self):
        raise JmftsError(status_code=401, detail="Invalid API token", url="/", method="GET")

    monkeypatch.setattr(JmftsClient, "health", _raise_401)
    with pytest.raises(StoreError, match="401"):
        build_jmfts_client({"session_store": {"url": "http://jmfts.test", "token": "wrong"}})


# ── host_parent_id validation ────────────────────────────────────────────────


def test_host_parent_id_none_by_default():
    assert resolve_host_parent_id({}) is None


def test_host_parent_id_reads_int():
    assert resolve_host_parent_id({"session_store": {"parent_id": 42}}) == 42


def test_host_parent_id_rejects_non_int():
    with pytest.raises(StoreError, match="parent_id"):
        resolve_host_parent_id({"session_store": {"parent_id": "42"}})
