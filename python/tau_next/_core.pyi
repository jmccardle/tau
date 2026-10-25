"""Type stubs for the native module built from crates/tau-py.

Hand-written in Phase 0. `python -m mypy.stubtest tau_next._core` (run by
scripts/check.sh) fails when this file and the built module disagree.
"""

from typing import final

__all__ = ["Probe", "ProbeError", "echo"]

class ProbeError(ValueError): ...

@final
class Probe:
    def __new__(cls, id: int, label: str, parts: list[str] = ...) -> Probe: ...
    @property
    def id(self) -> int: ...
    @property
    def label(self) -> str: ...
    @property
    def parts(self) -> list[str]: ...
    def digest(self) -> int: ...

def echo(probe: Probe) -> Probe: ...
