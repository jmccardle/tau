"""Ingest the STALE benchmark into JMFTS. M2 determination (b), staleness.

Tau-side POLICY: JMFTS supplies the mechanism (documents, a domain-time clock, hybrid
retrieval); how a benchmark corpus maps onto it is our opinion, not the substrate's.
See the mechanism/policy split in docs/RESEARCH-INTEGRATION-EVALUATION.md.

Corpus shape (verified against all 400 instances):
    400 instances x exactly 50 sessions, each session a list of {role, content} turns.
    `timestamps[i]` is session i's wall-clock time, monotonically increasing.
    `relevant_session_index` is always [old_idx, new_idx] with old_idx < new_idx:
    the session stating M_old, and the later one that implicitly invalidates it.

Mapping:
    one root per instance (usetype='stale_instance'), 50 session children under it.
    Each child carries `event_time = timestamps[i]` — the whole point of the exercise.
    Without it every session shares one ingest `created_at` and a recency term would
    be ranking ingest order, which is noise.

Embedding:
    `embed_tokens=False` is mandatory, not an optimisation: 19,999 of the 20,000
    sessions exceed the 512-token maxsim window (median ~3.6k tokens), and the
    embedder refuses over-window text rather than truncating it to a plausible-looking
    prefix. All 20,000 fit the 8192-token document-vector window, so every session
    still gets a full doc vector — which is what retrieval scores on.

Run ON midlife (needs the GPU embedder and the GPU-dev DB):
    /home/john/jmfts-gpudev-env.sh python3 ingest_stale.py [--limit N]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from jmfts_core.database import get_session_factory
from jmfts_core.repositories.document import DocumentRepository

STALE_PATH = "/fast/datasets/m2/STALE_T1_T2_400_FULL.json"
ROOT_USETYPE = "stale_instance"
SESSION_USETYPE = "stale_session"


def parse_timestamp(raw: str) -> datetime:
    """'2021-06-15 10:30' -> aware UTC.

    The corpus carries no zone. Reading it as UTC is a stated convention, not a
    discovered fact — it is consistent across every instance, and only *relative*
    ordering within an instance drives the determination, so a uniform offset cannot
    change any result.
    """
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def session_text(session: list[dict]) -> str:
    return "\n".join(f"{turn['role']}: {turn['content']}" for turn in session)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="ingest only the first N instances")
    ap.add_argument("--path", default=STALE_PATH)
    args = ap.parse_args()

    with open(args.path) as fh:
        corpus = json.load(fh)
    if args.limit:
        corpus = corpus[: args.limit]

    factory = get_session_factory()
    manifest = []
    started = time.time()
    docs_written = 0

    for n, instance in enumerate(corpus, 1):
        with factory() as db:
            repo = DocumentRepository(db)
            root = repo.create(
                title=f"STALE {instance['uid']}",
                content=None,
                usetype=ROOT_USETYPE,
                structured_content={
                    "uid": instance["uid"],
                    "type": instance["type"],
                    "explanation": instance["explanation"],
                    "M_old": instance["M_old"],
                    "M_new": instance["M_new"],
                },
                auto_embed=False,
            )
            db.flush()

            session_ids = []
            for idx, (session, raw_ts) in enumerate(
                zip(instance["haystack_session"], instance["timestamps"])
            ):
                child = repo.create(
                    title=f"session {idx} @ {raw_ts}",
                    content=session_text(session),
                    parent_id=root.id,
                    usetype=SESSION_USETYPE,
                    structured_content={"session_index": idx, "raw_timestamp": raw_ts},
                    event_time=parse_timestamp(raw_ts),
                    auto_embed=True,
                    embed_tokens=False,  # every session is over the 512-token maxsim cap
                    sequential=True,
                )
                session_ids.append(child.id)
                docs_written += 1

            old_idx, new_idx = instance["relevant_session_index"]
            manifest.append(
                {
                    "uid": instance["uid"],
                    "type": instance["type"],
                    "root_id": root.id,
                    "session_ids": session_ids,
                    "old_idx": old_idx,
                    "new_idx": new_idx,
                    "old_doc_id": session_ids[old_idx],
                    "new_doc_id": session_ids[new_idx],
                    "probing_queries": instance["probing_queries"],
                    # The agent asks its question at the end of the timeline; decay is
                    # measured from there, not from today. Real "now" is years past the
                    # corpus, which would flatten every session to recency ~0 and make
                    # the term measure nothing.
                    "now": instance["timestamps"][-1],
                }
            )
            db.commit()

        if n % 20 == 0:
            rate = docs_written / (time.time() - started)
            print(
                f"  {n}/{len(corpus)} instances, {docs_written} docs, {rate:.1f} docs/s", flush=True
            )

    with open("/fast/datasets/m2/stale_manifest.json", "w") as fh:
        json.dump(manifest, fh)

    elapsed = time.time() - started
    print(f"ingested {len(manifest)} instances / {docs_written} session docs in {elapsed:.0f}s")
    print(f"manifest: /fast/datasets/m2/stale_manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
