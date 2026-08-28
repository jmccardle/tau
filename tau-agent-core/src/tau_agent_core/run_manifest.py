"""The τ half of a measurement run's ``manifest.json`` (SIM_SPEC_v2 §9, §16.8).

§9 makes ``manifest.json`` the reproducibility artifact for a run. Two of its keys
are τ's to state, and they belong beside each other:

``harness``
    §5.2: assembled turn latency "is harness-dependent — under ``pi`` every tool
    call forks ``bash``→``python3``→``nats``, under τ it is an ``await``. …no
    pi-era latency is a baseline for a τ-era number."

``compaction``
    §16.8: the policy under which the run was taken. "'Leave the default' is not an
    option, and the choice goes in ``manifest.json`` beside ``harness``."

The two sit together because they are the same kind of key. This program has now
named five of them — §15.1's ``producer``, §8's ``harness``, ``Trace.arm``, freeze
v1.3's ``spool_capacity``, and this — and the sentence that generalises them is
tectum's: *a reconciliation number computed against a spool the production link does
not have is measuring the harness.* **A configuration that changes what a number
means is a mandatory partition key, not a footnote.**

"Mandatory" is enforced rather than asserted: :func:`require_compaction_policy`
refuses a manifest that lacks the key, and there is no default to fall back to. Two
runs will be compared whether or not their manifests permit it, so the only useful
place to stop an unlabelled run is before it produces numbers.

Consequences for other subsystems (§16.8, not this package's to fix)
----------------------------------------------------------------------

Two downstream effects of a ``turn_cap``-or-``local_summarizer`` compaction firing
mid-run are outside this package and are recorded here rather than silently
discovered by whoever hits them:

1. **jmfts.** ``D7`` is computed from the session tree, not from the live
   in-memory context. A compaction re-parents the tree (see the compaction
   surgery in ``agent_session.py``), so a ``D7`` measurement taken after a
   compaction is reading a different tree shape than one taken before — the same
   §9-rule-1 pattern this module exists to price, one layer further out.
2. **tectum.** A bus-side node correlating on the ``agent_start``/``agent_end``
   pair (the same bracket :mod:`tau_agent_core.latency` uses as the compaction
   marker) will see a turn that produced no visible output and took several
   seconds — indistinguishable, from the bus side, from a hang, unless the node
   also knows the bracket can mean "compaction happened here."
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tau_agent_core.compaction_policy import CompactionPolicy
from tau_llm.docs import agent_facing

#: The value of ``harness`` for a run driven by this package. §5.2 partitions every
#: latency number by it; a pi-era number and a τ-era number are different
#: populations and must never be pooled.
HARNESS = "tau"


@agent_facing(topic="runs")
def extension_manifest_entries(infos: Sequence[Any]) -> list[dict[str, Any]]:
    """The ``extensions`` manifest fragment (H7, SIM_SPEC_v2 §16.6).

    ``infos`` is whatever :func:`~tau_agent_core.sdk.summarize_extensions`
    returned — accepted structurally (``name``/``path``/``content_hash``/
    ``subjects``/``tools``/``commands``/``shortcuts``/``hooks`` attributes)
    rather than by importing :class:`~tau_agent_core.sdk.ExtensionInfo`, so this
    module does not have to import ``sdk`` (which imports ``agent_session``,
    which this package's ``__init__`` already loads before ``run_manifest``).

    Two runs against the same extension path at different file contents
    produce different ``content_hash`` entries here — that is the entire point
    (§16.6: "two runs ... at different contents are two experimental
    conditions carrying one label").

    Provisional shape: §16.6 asks this be built to the same diff format as
    tectum's ``T4`` (``tectum-005``), which had not landed as of this writing.
    Coordinated through the format when it exists, not guessed at here.
    """
    return [
        {
            "name": info.name,
            "path": info.path,
            "content_hash": info.content_hash,
            "subjects": list(info.subjects),
            "tools": list(info.tools),
            "commands": list(info.commands),
            "shortcuts": list(info.shortcuts),
            "hooks": list(info.hooks),
        }
        for info in infos
    ]


@agent_facing(topic="runs")
def build_run_manifest(
    *,
    harness: str = HARNESS,
    compaction_policy: CompactionPolicy,
    extensions: Sequence[Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the manifest fragment this harness is responsible for.

    ``compaction_policy`` is keyword-only and has no default, so a manifest cannot
    be built without making the §16.8 decision. That is the whole mechanism: the
    option "leave the default" is removed by there not being one.

    Args:
        harness: the harness identity §5.2 partitions latency by. Defaults to
            :data:`HARNESS`; a pi-era run records ``"pi"`` and the two are different
            populations that must never be pooled.
        compaction_policy: the run's declared policy.
        extensions: the run's loaded extensions, as returned by
            ``sdk.summarize_extensions()`` (H7, §16.6). ``None`` omits the
            ``extensions`` key entirely rather than writing an empty list, so a
            manifest built by a caller that hasn't threaded this through yet is
            visibly missing the key rather than silently claiming zero
            extensions loaded.
        **extra: further top-level manifest keys the caller owns (seed, run id,
            clock origin, DB snapshot id — §9's list). Passed through unchanged and
            refused if they would shadow a key this function owns.

    Raises:
        ValueError: ``extra`` contains ``compaction`` or ``extensions``.
    """
    owned = {"compaction", "extensions"} & extra.keys()
    if owned:
        raise ValueError(
            f"{sorted(owned)} owned by build_run_manifest and cannot be passed as extra; "
            "a manifest with two answers for a partition key has none"
        )
    manifest: dict[str, Any] = {
        "harness": harness,
        "compaction": compaction_policy.to_manifest(),
    }
    if extensions is not None:
        manifest["extensions"] = extension_manifest_entries(extensions)
    manifest.update(extra)
    return manifest


@agent_facing(topic="runs")
def require_compaction_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the manifest's compaction block, or raise.

    The read side of the mandatory-partition-key rule. A consumer that reaches for
    the policy and finds nothing must stop, not assume: a run whose policy was not
    recorded cannot be compared to any other run, and "probably the default" is the
    guess §16.8 exists to prevent.

    Raises:
        KeyError: ``compaction`` is absent, or ``harness`` is absent — the two are
            reported together because a compaction policy without a harness
            identity is only half a partition key.
        ValueError: the block is present but not a mode-bearing mapping.
    """
    missing = [key for key in ("harness", "compaction") if key not in manifest]
    if missing:
        raise KeyError(
            f"manifest is missing {missing}; a run whose harness and compaction policy "
            "are not recorded cannot be compared to any other run, and it will be "
            "compared anyway (§16.8, §5.2)"
        )
    block = manifest["compaction"]
    if not isinstance(block, dict) or "mode" not in block:
        raise ValueError(
            f"manifest['compaction'] is not a policy record: {block!r}. Expected the "
            "mapping produced by CompactionPolicy.to_manifest()"
        )
    return block


@agent_facing(topic="runs")
def write_run_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    """Write ``manifest`` as JSON, refusing one that is not partitionable.

    The check runs on the way out rather than being left to the reader, so an
    unlabelled manifest never reaches disk to be trusted later.
    """
    require_compaction_policy(manifest)
    target = Path(path)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
