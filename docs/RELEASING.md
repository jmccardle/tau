# Cutting a τ release

Written 2026-08-20 while cutting 0.9.2, from what the release actually did.
Every command here was run and every value was read off the thing it configures.

Revised 2026-08-28 while cutting 0.9.5, which closed the two things this document
still did not know. The TestPyPI rehearsal in step 6 had never run; it has now,
and it uploaded. The final `git push origin vX.Y.Z` in step 7 was wrong and is
removed — publishing the draft already creates that tag.

## The two repositories

| | remote | history | what it holds |
|---|---|---|---|
| `agent-harness-py` | `origin` → the private git host | full, unfiltered | development |
| `tau_public` | `origin` → `github.com/jmccardle/tau.git` | one commit per release | what the world sees |

The public repository is **not** a mirror with a filtered history. It is a
squash: each release replaces its whole tree in a single commit whose subject is
`vX.Y.Z - <headline>`. `git log` there is three commits long after 0.9.2.

The internal repository can see both, because it also has a `github` remote. It
carries two tags per release:

* `vX.Y.Z` — the public squashed commit.
* `vX.Y.Z-fullhistory` — the internal commit that release was cut from.

Neither tag is pushed to `origin`; `git ls-remote --tags origin` is empty.

`CLAUDE.md` is the one tracked file the public tree does not get. That has been
true since v0.9.0. Everything else in internal `master` is published verbatim —
verified by comparing blob hashes, not filenames.

### A test may not assume the history it runs against

CI runs in the **public** repository, so it sees three commits. `test_e5_status_
doc.py` resolved commit shas cited in a doc via `git show -s`, passed here, and
failed all nine of its cases there — blocking the pipeline, because `publish`
needs `test`. It was retired at 0.9.2 rather than made conditional.

Before adding a test that shells out to `git`, ask whether the commits it names
exist in a three-commit checkout. They will not.

The rule is about **history**, not about `git` itself — 0.9.2's blanket "no test
runs `git`" was wrong when it was written. Several tests build a throwaway repo
under `tmp_path`, which is self-contained and safe anywhere, and one queries the
repository the suite is running in: `test_no_host_addresses.py` reads `git
ls-files` to enumerate the tracked shipped files. That one is fine, because a
shallow squashed checkout still tracks every file — depth is what it does not
have. It does mean the suite needs a real checkout, so it fails outside a work
tree: a tarball, or an unpacked `git archive`, is not enough.

## Steps

### 1. Bump

```bash
./bump-version.sh 0.9.3
pytest tau-coding-agent/tests/test_packaging.py
git commit -am "release: version 0.9.3"
```

The version lives in thirteen places. The script writes all thirteen and refuses
to run on a dirty tree. Do not add a `version = "..."` literal to a package
`pyproject.toml` to fix a mismatch — a test forbids the second copy.

### 2. Gate

```bash
pytest                                  # 4164 passed, 135 skipped, 6 deselected at 0.9.2
venv/bin/ruff check tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src tau-meta/src
venv/bin/ruff format --check tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src tau-meta/src
venv/bin/mypy tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src tau-meta/src
```

Those are the four `.githooks/pre-commit` runs. `ruff check .` over the whole
repo reports findings in `tests/`, `experiments/` and `run_agent_loop.py`; those
trees are outside the gate on purpose, so run ruff on the five `src` trees, not
on `.`.

#### Run the matrix BEFORE the tag push, not after

The local gate is 3.11 only, and `publish.yml` tests 3.11, 3.12, 3.13 and 3.14
with `fail-fast: true`. So the first time a release learns about the other
three is the tag push — and a tag push is the one step that cannot be taken
back. 0.9.3 was tagged, went red on 3.13, and had to be recut.

Run all four locally first:

```bash
tar -cf /tmp/src.tar --exclude=./venv --exclude=__pycache__ \
    --exclude='*.egg-info' --exclude=./.git --exclude='*.tar.gz' .
for v in 3.11 3.12 3.13 3.14; do
  docker run --rm -v /tmp/src.tar:/src.tar:ro python:$v-bookworm bash -c '
    mkdir /work && tar -xf /src.tar -C /work && cd /work &&
    pip install -q -e ./tau-llm -e "./tau-agent-core[dev]" \
                   -e "./tau-coding-agent[dev]" -e ./tau-jmfts &&
    python -m pytest -q' | tail -3
done
```

**Read 3.11 as the control, not as a pass.** Four tests fail in this harness on
every version, and they are artifacts of it rather than defects:

| Test | Why it fails here |
|---|---|
| `test_no_host_addresses_in_shipped_trees` | shells out to `git ls-files`; the tar has no `.git` |
| `test_every_install_hint_names_a_real_distribution` | same |
| `TestBashToolProcessGroupKill::test_timeout_kills_backgrounded_grandchild` | no real init to reap a process group |
| `TestBashToolProcessGroupKill::test_abort_kills_backgrounded_grandchild` | same |

A version is clean when its output is **identical to 3.11's**. At 0.9.3 all
four gave `4551 passed, 4 failed, 151 skipped`. Anything else is a real
difference and blocks the tag.

The two failures that actually cost 0.9.3 its first tag are worth knowing,
because both are shapes a 3.11-only gate cannot see:

* **`Server.wait_closed()` changed in CPython 3.12** and now waits for open
  connections' handlers to finish. A test whose handler stalls forever hangs
  its own teardown. Measured: returns on 3.11, hangs on 3.13.
* **Callback ordering after `asyncio.wrap_future`.** A coroutine that resolves
  a `concurrent.futures.Future` from inside itself resolves it *before* the
  task is done, so the task's done-callback has not run when the awaiting
  caller wakes. How many loop iterations separate the two is the scheduler's
  business, and it changed.

### 3. Merge and push internally

```bash
git checkout master
git merge --ff-only <branch>
git push origin master
```

### 4. Replace the public tree

From a clean `tau_public` checkout that matches `origin/master`:

```bash
cd ~/Development/tau_public
git ls-files -z | xargs -0 rm -f
find . -mindepth 1 -depth -type d -not -path "./.git*" -empty -delete
git -C ~/Development/agent-harness-py archive master | tar -x -C .
rm -f CLAUDE.md
git add -A
```

Deleting every tracked file first is what makes a rename or a deletion land;
extracting over the old tree would leave the removed files behind. Then confirm
the result is the internal tree and nothing else:

```bash
git ls-files -s | awk '{print $2, $4}' | sort > /tmp/pub
git -C ~/Development/agent-harness-py ls-tree -r master | awk '{print $3, $4}' \
  | grep -v " CLAUDE.md$" | sort > /tmp/int
diff /tmp/int /tmp/pub
```

Commit and tag with the same message, then push the branch:

```bash
git commit -F <notes>
git tag -a v0.9.3 -F <notes>
git push origin master
```

**Do not push the tag yet.** See step 6.

Back in the internal repository:

```bash
git fetch github
git tag -a v0.9.3 -m "..." <public commit>
git tag -a v0.9.3-fullhistory -m "..." <internal master commit>
```

### 5. Build and check

```bash
pip install build twine
for pkg in tau-llm tau-agent-core tau-coding-agent tau-jmfts tau-meta; do
    python -m build --sdist --wheel --outdir "$DIST" "./$pkg"
done
twine check --strict "$DIST"/*
./package.sh                            # the tau-<version>.tar.gz, three packages, no tau-jmfts
```

Ten artifacts: five projects, a wheel and an sdist each. `tau-meta` is the
`ffwf-tau` metapackage — no functional code, one dependency on
`ffwf-tau-coding-agent[tui]`. It is not in the `package.sh` tarball, which stages
functional source only.

**Neither build path may patch source.** The two lines above are two independent
build paths over one tree, and they may differ only in *shape* — which projects
they carry (five vs. three: the tarball has no `tau-jmfts` and no `tau-meta`),
and what rides along (the wheels carry PyPI metadata; the tarball carries none).
They may not differ in the content of a shared source file. `package.sh` used to
`sed` `tau_coding_agent/tagline.py` to turn `--fun` on for a release, and because
the wheels come from `python -m build` and never ran it, every published wheel
shipped the developer default — the feature reached only the artifact nobody
installs from. The rewrite is gone, the default lives in the source, and
`test_chat_placeholder.py::test_no_build_path_rewrites_the_fun_default` fails the
suite if either file grows one again. If a release ever needs a value to differ
between a checkout and a build, put it in the source and have the checkout ask
for the other one — not the reverse.

Build to a directory outside the repository. `dist/` is gitignored as of 0.9.3,
so a build inside the tree no longer leaves an untracked directory behind — but
outside is still better, because `$DIST` also collects the `package.sh` tarball
and the two are easier to keep straight when neither is in the working tree.

Then install the wheels into a throwaway venv and run them. `twine check` reads
metadata; it does not tell you the package works:

```bash
python3 -m venv /tmp/smoke
/tmp/smoke/bin/pip install --find-links "$DIST" "ffwf-tau-coding-agent[tui,jmfts]==0.9.3"
/tmp/smoke/bin/tau --version
/tmp/smoke/bin/ffwf-tau --version
```

Smoke the metapackage in its own venv, because what it is for is the resolution
it triggers — a stale pin inside it is invisible in every other check:

```bash
python3 -m venv /tmp/smoke-meta
/tmp/smoke-meta/bin/pip install --find-links "$DIST" ffwf-tau
/tmp/smoke-meta/bin/tau --version                       # the command exists
/tmp/smoke-meta/bin/python -c "import textual"          # [tui] came with it
/tmp/smoke-meta/bin/python -c "import tau_jmfts"        # must FAIL: [jmfts] must not
```

Do not pass `--no-index`: the τ wheels are local but their third-party
dependencies are not. Also install the plain, no-extras form and check that
`tau` refuses to start the TUI with a named error rather than a traceback.

Check the two runtime data files are inside the installed package:

```python
import importlib.resources as r, tau_coding_agent
r.files(tau_coding_agent).joinpath("tau_default_config.json").is_file()
r.files(tau_coding_agent).joinpath("parley.tcss").is_file()
```

A wheel missing either installs cleanly and dies on first run.

### 6. Publish

`.github/workflows/publish.yml` does the upload, through PyPI Trusted
Publishing. There is no API token anywhere.

**The publish path is proven, as of run 32473936746 (2026-08-21).** Publishing
the v0.9.2 GitHub release created the tag, which fired the workflow. It reached
`pypa/gh-action-pypi-publish`, and the log shows the action holding a credential
it did not have a moment earlier:

```
INFO  username: __token__
INFO  password: <hidden>
Uploading ffwf_tau_agent_core-0.9.2-py3-none-any.whl
INFO  Response from https://upload.pypi.org/legacy/: 400 Bad Request
      400 File already exists (…)
```

That token is minted by exchanging the job's OIDC identity with PyPI, so those
two lines are the proof the exchange worked — owner, repository, workflow
filename and environment name all matched a registered publisher. The upload
then failed for the only reason left: 0.9.2 was already on PyPI from the hand
bootstrap, and `skip-existing` is deliberately off.

So the whole pipeline is exercised except the final write, and the one step that
could still surprise a release is the one that only succeeds once per version.
Read a `400 File already exists` as success of everything before it.

**Wheels are not byte-reproducible across machines.** The same run shows it: the
CI-built wheel hashed `789ca4e4…` while the published one is `40f090ac…`, same
version, same source. Ordinary — a wheel embeds timestamps and build paths — but
it means the artifact on PyPI is whichever machine uploaded first, and for 0.9.3
that should be CI rather than a laptop.

**A project that does not exist on the index yet cannot be created by this
workflow.** PyPI will not accept two *pending* publishers that share one
configuration — the second attempt is refused with:

> A pending trusted publisher matching this configuration has already been
> registered for a different project name.

This constraint applies to pending rows only. Once a project exists, the same
identity may be registered to all of them, which is what the workflow's header
comment describes and what every release after the first one uses. So the
comment is right about steady state and wrong about the bootstrap.

**This is a per-project cost, and it recurs.** It was paid four times for 0.9.2
and once more for `ffwf-tau`, the metapackage added after 0.9.3 was already
published. A distribution added between releases can be bootstrapped on its own,
without cutting a new version of anything: build only its two artifacts, hand
upload them at the current lockstep version, then register its normal publisher
so the next tag push carries it automatically.

Bootstrap each index **once**, by hand, with an account-scoped API token:

```bash
cat > ~/.pypirc <<'EOF'
[distutils]
index-servers = testpypi

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = <the token>
EOF
chmod 600 ~/.pypirc
twine upload --repository testpypi "$DIST"/ffwf_*
shred -u ~/.pypirc                      # then REVOKE the token on the index
```

The `ffwf_*` glob is not cosmetic: `$DIST` also holds `tau-<version>.tar.gz`
from `package.sh`, which carries no PyPI metadata and is not a distribution.

**A TestPyPI install of `ffwf-tau` needs the real index too.** TestPyPI carries
`ffwf-tau-coding-agent` at 0.9.2 only, so the metapackage's `==0.9.3` pin cannot
resolve there on its own:

```bash
/tmp/smoke-meta/bin/pip install \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ ffwf-tau
```

That is not a workaround for a defect. The TestPyPI upload rehearses the upload
and the publisher registration; the dependency is pulled from the index that
actually has it. The resolution itself is what the local `--find-links` smoke in
step 5 proves.

Drop `--repository testpypi` and point `repository` at
`https://upload.pypi.org/legacy/` for real PyPI. The token is used locally and
never becomes a repository secret, so "no API token in CI" still holds.

After the bootstrap upload, delete the now-stale pending publisher — it created
nothing and will never convert — and register a **normal** trusted publisher per
project:

* PyPI — <https://pypi.org/manage/account/publishing/>
* TestPyPI — <https://test.pypi.org/manage/account/publishing/>

Five projects on each index, ten registrations, all with the same three
middle fields:

| Field | Value |
|---|---|
| PyPI project name | `ffwf-tau-llm`, `ffwf-tau-agent-core`, `ffwf-tau-coding-agent`, `ffwf-tau-jmfts`, `ffwf-tau` |
| Owner | `jmccardle` |
| Repository name | `tau` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` on PyPI, `testpypi` on TestPyPI |

The two GitHub environments already exist and hold no secrets and no protection
rules. The environment name is part of the publisher identity, so it has to
match exactly.

Rehearse on TestPyPI first — the only way to find out whether the ten
registrations are right before a tag push makes it expensive:

```bash
gh workflow run publish.yml --repo jmccardle/tau --ref master -f target=testpypi
```

**The rehearsal path is proven, as of run 33179990481 (2026-08-28, 0.9.5).** This
document said for two releases that this run had not happened yet. It has now,
and it *wrote*: all ten artifacts uploaded to TestPyPI and the job reported
success, so the five TestPyPI publishers are correct and the OIDC exchange works
on that index too. Unlike the PyPI proof above — which is a `400 File already
exists` read as success of everything before it — this one is an actual upload.

The same run also confirms the guard: `Publish to PyPI` reported `skipped`,
because its condition is `github.event_name == 'push' || inputs.target == 'pypi'`
and a dispatch is not a push.

A TestPyPI version cannot be reused either, so a rehearsal costs the version it
rehearses. Run it once per release, after the local gate, not while iterating.

It runs the same test and build jobs as a real release. `publish-pypi` is
skipped, because its condition is `github.event_name == 'push' || inputs.target
== 'pypi'` and a dispatch is not a push — so a rehearsal cannot reach real PyPI
even if the target is wrong.

### 7. The GitHub release

Each release also gets a GitHub Release carrying `tau-<version>.tar.gz`. The
wheels and sdists are not attached: PyPI is their home, CI rebuilds them at
publish time, and a second copy here could differ from what was published.

Create it as a **draft** while the publisher registration is still outstanding:

```bash
gh release create v0.9.3 --repo jmccardle/tau --draft \
  --target "$(git rev-parse master)" --title "v0.9.3" \
  --notes-file <notes> "$DIST/tau-0.9.3.tar.gz"
```

A draft creates no tag, so nothing is triggered — the notes and the asset sit
staged until you publish it. `--target` wants a branch name or a **full** SHA;
an abbreviated one is rejected with `Release.target_commitish is invalid`.

Publishing the draft creates the tag, and that tag push is the real release. It
is the only step that reaches real PyPI.

```bash
gh release edit v0.9.5 --repo jmccardle/tau --draft=false
```

**Do not then push the local tag.** Every version of this document up to 0.9.5
ended with `cd ~/Development/tau_public && git push origin vX.Y.Z`. That step is
wrong, and it has been wrong since drafts were introduced: publishing the draft
already created `refs/tags/vX.Y.Z` on the remote, through the API, as a
**lightweight** tag pointing straight at the commit. The local tag was made with
`git tag -a`, so it is a tag *object* — a different sha for the same commit — and
the push is rejected:

```
 ! [rejected]        v0.9.5 -> v0.9.5 (already exists)
```

Measured at 0.9.5, and `git ls-remote --tags origin` shows 0.9.4 has the same
shape, so it happened there too and nobody wrote it down. Only `v0.9.3` carries a
`^{}` peeled line, from the era when the tag really was pushed by hand.

Do **not** `--force` past this. The remote tag names the right commit, the
workflow has already run from it, and replacing it would move a published release
ref to make two objects agree about a commit they already agree about. Leave the
annotated tag local; it is the internal record, alongside `vX.Y.Z-fullhistory`.

A PyPI version number cannot be reused, and `skip-existing` is deliberately left
off, so a second attempt at the same version fails rather than quietly doing
nothing. Re-releasing means a new version.

`on: push: tags: v*` also matches `v0.9.3-fullhistory`. That tag is internal and
is never pushed to GitHub; if one ever were, the build job's tag-versus-package
comparison fails before any publish job runs.
