"""Phase 0: a Rust struct round-trips through Python.

Deleted with tau_core::scaffold when Phase 3 lands real types.
"""

import struct

import pytest

from tau_next import Probe, ProbeError, echo


def fnv1a(probe: Probe) -> int:
    """Independent reimplementation of tau_core::scaffold::Probe::digest."""
    h = 0xCBF29CE484222325

    def write(b: bytes) -> None:
        nonlocal h
        for x in b:
            h = ((h ^ x) * 0x100000001B3) & (2**64 - 1)

    def write_str(s: str) -> None:
        b = s.encode()
        write(struct.pack("<Q", len(b)))
        write(b)

    write(struct.pack("<Q", probe.id))
    write_str(probe.label)
    write(struct.pack("<Q", len(probe.parts)))
    for part in probe.parts:
        write_str(part)
    return h


def test_round_trip_preserves_fields() -> None:
    p = Probe(7, "seven", ["a", "b"])
    q = echo(p)
    assert q is not p
    assert q == p
    assert (q.id, q.label, q.parts) == (7, "seven", ["a", "b"])


def test_digest_matches_independent_implementation() -> None:
    p = Probe(7, "seven", ["a", "b"])
    assert p.digest() == fnv1a(p) == 0xF5C4FE255F721547


def test_unicode_and_u64_extremes_cross_intact() -> None:
    p = Probe(2**64 - 1, "τ", ["", "🦀", "\x00"])
    assert echo(p).parts == ["", "🦀", "\x00"]
    assert p.digest() == fnv1a(p)


def test_out_of_range_id_is_a_python_error() -> None:
    with pytest.raises(OverflowError):
        Probe(2**64, "x")
    with pytest.raises(OverflowError):
        Probe(-1, "x")


def test_core_error_maps_to_typed_exception() -> None:
    with pytest.raises(ProbeError, match="must not be empty") as info:
        Probe(1, "")
    assert isinstance(info.value, ValueError)


def test_probe_is_immutable() -> None:
    p = Probe(1, "x")
    with pytest.raises(AttributeError):
        p.label = "y"  # type: ignore[misc]
    p.parts.append("leak")
    assert p.parts == []
