"""SessionLog conformance — InMemorySessionLog (the SDK-default store)."""

from __future__ import annotations

from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.testing import SessionLogContractTests


class TestInMemorySessionLogContract(SessionLogContractTests):
    def make_log(self):
        return InMemorySessionLog()
