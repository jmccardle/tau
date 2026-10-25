# parity/

Migration tooling, not part of τ. It is deleted when this project is
absorbed into the τ repository as 0.16; `tests/` is carried over and replaces
the 0.15 suite.

τ 0.16 is incompatible with 0.15 on purpose. These tests are the API diff:
they drive τ 0.15 (the *oracle*) and `tau_next` through the same scenarios and
record, per scenario, whether 0.16 **keeps** 0.15's behavior, **changes** it
(citing why), **drops** it, or is **new**.

| | `tests/` | `parity/` |
|---|---|---|
| needs | `--group dev` | `--group dev --group parity` (adds `ffwf-tau==0.15.0`) |
| runs | `scripts/check.sh` | `scripts/parity.sh` |
| must pass | with no τ 0.15 installed anywhere | with both installed in one venv |
| fate at 0.16 | becomes τ's test suite | deleted |

Nothing under `tests/` or `python/` may import from here.
