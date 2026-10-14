"""Code mode: one ``codemode`` tool that runs a script, with τ's tools inside it.

The implementation lives in ``tau_agent_core.codemode``; this file is the
loadable entry point, and its stem is the config key::

    tau -e examples/codemode.py -p "..."

``~/.tau/config.json``::

    {"extensions": {"codemode": {"provider": "monty", "lifetime": "session"}}}

or per run: ``--ext-config codemode.provider=monty``. The Monty provider needs
``pip install 'ffwf-tau-agent-core[codemode-monty]'``.
"""

from tau_agent_core.codemode.extension import CONFIG_SCHEMA, register

__all__ = ["CONFIG_SCHEMA", "register"]
