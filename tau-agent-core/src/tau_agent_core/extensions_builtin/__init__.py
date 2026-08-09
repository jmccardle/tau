"""Reference extensions shipped with tau-agent-core.

Unlike the runtime machinery in :mod:`tau_agent_core.extensions` (the runner,
registry, hook dispatch), everything under this package is an ordinary FILE
extension — it is loaded through the same ``AgentSession.load_extensions``
path as anything a user drops in ``~/.tau/extensions``, using
``Path(__file__)`` as the entry point rather than an import. Shipping it here
(rather than only as documentation) keeps it exercised by the real test suite
instead of drifting from whatever the loader actually does.
"""
