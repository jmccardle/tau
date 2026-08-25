"""The version literal for the ``ffwf-tau`` metapackage, and nothing else.

``ffwf-tau`` ships no functional code: it is a name that resolves to
``ffwf-tau-coding-agent[tui]``. This module exists so that its wheel version is
read from a package attribute like every other distribution in this tree, rather
than from a second copy of the number written into a pyproject.

Nothing imports this. ``tau_coding_agent.__version__`` is the version the running
program reports.
"""

__version__ = "0.9.4"
