# Cutting a τ release

Written 2026-08-20 while cutting 0.9.2, from what the release actually did.
Every command here was run; every value was read off the thing it configures.

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
exist in a three-commit checkout. They will not. As of 0.9.2 no test in the
suite runs `git`, and that is worth keeping true.

## Steps

### 1. Bump

```bash
./bump-version.sh 0.9.3
pytest tau-coding-agent/tests/test_packaging.py
git commit -am "release: version 0.9.3"
```

The version lives in eleven places. The script writes all eleven and refuses to
run on a dirty tree. Do not add a `version = "..."` literal to a package
`pyproject.toml` to fix a mismatch — a test forbids the second copy.

### 2. Gate

```bash
pytest                                  # 4164 passed, 135 skipped, 6 deselected at 0.9.2
venv/bin/ruff check tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src
venv/bin/ruff format --check tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src
venv/bin/mypy tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src
```

Those are the four `.githooks/pre-commit` runs. `ruff check .` over the whole
repo reports findings in `tests/`, `experiments/` and `run_agent_loop.py`; those
trees are outside the gate on purpose, so run ruff on the four `src` trees, not
on `.`.

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
for pkg in tau-llm tau-agent-core tau-coding-agent tau-jmfts; do
    python -m build --sdist --wheel --outdir "$DIST" "./$pkg"
done
twine check --strict "$DIST"/*
./package.sh                            # the tau-<version>.tar.gz, three packages, no tau-jmfts
```

Build to a directory outside the repository. `dist/` has no `.gitignore` rule,
so a build inside the tree leaves an untracked directory behind. (`tau-*/build/`
*is* ignored, which is why the per-package build dirs do not show up.)

Then install the wheels into a throwaway venv and run them. `twine check` reads
metadata; it does not tell you the package works:

```bash
python3 -m venv /tmp/smoke
/tmp/smoke/bin/pip install --find-links "$DIST" "ffwf-tau-coding-agent[tui,jmfts]==0.9.3"
/tmp/smoke/bin/tau --version
/tmp/smoke/bin/ffwf-tau --version
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

**A project that does not exist on the index yet cannot be created by this
workflow.** PyPI will not accept four *pending* publishers that share one
configuration — the second attempt is refused with:

> A pending trusted publisher matching this configuration has already been
> registered for a different project name.

This constraint applies to pending rows only. Once a project exists, the same
identity may be registered to all four, which is what the workflow's header
comment describes and what every release after the first one uses. So the
comment is right about steady state and wrong about the bootstrap.

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

Drop `--repository testpypi` and point `repository` at
`https://upload.pypi.org/legacy/` for real PyPI. The token is used locally and
never becomes a repository secret, so "no API token in CI" still holds.

After the bootstrap upload, delete the now-stale pending publisher — it created
nothing and will never convert — and register a **normal** trusted publisher per
project:

* PyPI — <https://pypi.org/manage/account/publishing/>
* TestPyPI — <https://test.pypi.org/manage/account/publishing/>

Four projects on each index, eight registrations, all with the same three
middle fields:

| Field | Value |
|---|---|
| PyPI project name | `ffwf-tau-llm`, `ffwf-tau-agent-core`, `ffwf-tau-coding-agent`, `ffwf-tau-jmfts` |
| Owner | `jmccardle` |
| Repository name | `tau` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` on PyPI, `testpypi` on TestPyPI |

The two GitHub environments already exist and hold no secrets and no protection
rules. The environment name is part of the publisher identity, so it has to
match exactly.

Rehearse on TestPyPI first. Register the four TestPyPI publishers, then:

```bash
gh workflow run publish.yml --repo jmccardle/tau --ref master -f target=testpypi
```

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

Publishing the draft creates the tag, and that tag push is the real release.

Then push the tag, which is the only step that reaches real PyPI:

```bash
cd ~/Development/tau_public && git push origin v0.9.3
```

A PyPI version number cannot be reused, and `skip-existing` is deliberately left
off, so a second attempt at the same version fails rather than quietly doing
nothing. Re-releasing means a new version.

`on: push: tags: v*` also matches `v0.9.3-fullhistory`. That tag is internal and
is never pushed to GitHub; if one ever were, the build job's tag-versus-package
comparison fails before any publish job runs.
