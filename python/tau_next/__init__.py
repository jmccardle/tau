"""τ next ("Trusty"): Rust core, Python extensions."""

from importlib.metadata import version

from tau_next._core import Probe, ProbeError, echo

__version__ = version("ffwf-tau-next")

__all__ = ["Probe", "ProbeError", "__version__", "echo"]
