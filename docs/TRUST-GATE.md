# The trust gate (Tier 8)

**research record (2026-10-12), nothing built.** No `trust.json`, no `TrustGate`
symbol and no `--approve`/`--no-approve` flag exist. This record measures what
τ takes from a checkout today and proposes one shape. §6 lists the decisions
that only the repo owner can make, and nothing should be built before they are
answered.

## 1. What τ takes from an untrusted checkout today

This was measured on 2026-10-12 against `354f1f0`. The probe ran
`load_project_context_files` and `_load_extensions` on a scratch tree:
`clone/AGENTS.md`, `clone/sub/CLAUDE.md`, `clone/sub/.tau/SYSTEM.md` and
`clone/sub/.tau/extensions/evil.py`, whose body is
`raise SystemExit("EXECUTED")`. The probe's cwd was `clone/sub`.

| Resource | Today | Where |
|---|---|---|
| `AGENTS.md` / `CLAUDE.md` in cwd **and every ancestor up to `/`** | goes into the system prompt, with no question asked | `sdk.py:922` `load_project_context_files` |
| `<cwd>/.tau/SYSTEM.md` | goes into the system prompt, last, with no question asked | `sdk.py:985` |
| `<cwd>/.tau/extensions/*.py` | **never discovered**: `evil.py` did not run | `sdk.py:667`, global dir only |
| `<cwd>/.tau/settings.json` | read by `Settings.load`, which **has no caller** in any `src` tree | `settings.py:76` |
| skills, prompt templates | do not exist yet (Tier 10) | — |

The probe returned three context files: `clone/AGENTS.md`,
`clone/sub/CLAUDE.md` and `clone/sub/.tau/SYSTEM.md`. It loaded zero
extensions.

So today the gate has **no code to stop**. The project-local code path was left
out on purpose, and the reason was "until the trust gate ships"
(`EXTENSIONS-ORCHESTRATION-PLAN.md` §3.4, `extensions.md` §Discovery,
`ARCHITECTURE.md`). The work is about **turning on** project-local discovery
behind a decision. It does not close a hole that is open now. The only ungated
project-local files are text files, and the prompt labels each one with the
absolute path it came from (`sdk.py:1083`).

The table also shows a value with no reader, which is one of the three faults
the feature-doc skill names. `Settings.load` merges `{cwd}/.tau/settings.json`
and nothing calls it, so the project-local settings file it documents does
nothing. `Settings` is exported (`__init__.py:28`) and carries
`@agent_facing`.

## 2. What pi does (provenance, not authority)

pi's gate is in three files: `core/trust-manager.ts`, `core/project-trust.ts`
and `cli/project-trust.ts`.

- **What it gates.** pi gates `.pi/{settings.json, extensions, skills, prompts,
  themes, SYSTEM.md, APPEND_SYSTEM.md}` in cwd, plus `.agents/skills` in cwd or
  any ancestor. It does **not** gate `AGENTS.md` or `CLAUDE.md`. When none of
  these resources exists, the run counts as trusted and pi asks nothing.
- **Store.** The store is `~/.pi/trust.json`, which maps a canonical path to
  `true` or `false`. The nearest ancestor entry wins, so trusting `~/src` covers
  every checkout under it. The file is locked with `proper-lockfile`.
- **Order of resolution.** The `--approve`/`--no-approve` flag decides first.
  Next come "no gated resources → trusted", an extension `project_trust` event,
  the store, and a `defaultProjectTrust` setting (`ask`|`always`|`never`).
  Last, if there is a UI, pi shows a five-way prompt: trust, trust the parent
  folder, trust for this session, do not trust, do not trust for this session.
  With no UI, the result is **untrusted, silently**.

τ should keep two things from pi: the nearest-ancestor path store and the rule
that an explicit flag always wins. τ should drop the lockfile, because the
single-writer stance recorded with `TAU-SERVE.md` makes a lock on a
hand-edited JSON file the kind of machinery τ declines. It should also drop the
silent headless fallback (§5).

## 3. The proposed shape: one pure decision in `tau_agent_core`

τ has four places that would apply a decision: the Textual TUI (with
`--mode repl`), `tau -p`, `tau --mode rpc`, and the `tau serve` daemon
(`serve/daemon.py:327`). Each of them reaches project files through two core
choke points:

1. `_render_project_context` (`sdk.py:1083`), through `_build_system_prompt`.
2. `_load_extensions` (`sdk.py:596`), through `AgentSession.load_extensions`
   (`agent_session.py:1598`).

The proposal is a new module, `tau_agent_core/trust.py`, holding only pure
functions plus one file reader:

```python
GATED = (".tau/extensions", ".tau/SYSTEM.md", ...)     # §6 Q2 decides the list

@dataclass(frozen=True)
class TrustDecision:
    verdict: Literal["trusted", "untrusted", "undecided", "not_needed"]
    source: str                 # "flag" | "trust.json:<path>" | "policy" | "none"
    gated: tuple[Path, ...]     # what exists here and is waiting on the verdict

def gated_resources(cwd: Path) -> tuple[Path, ...]: ...
def read_trust_store(path: Path) -> dict[Path, bool]: ...   # raises on malformed JSON
def decide_trust(cwd, store, *, override: bool | None) -> TrustDecision: ...
def record_trust(path: Path, target: Path, decision: bool | None) -> None: ...
```

The heads would get `TrustDecision` and nothing else. A head may **render** it,
or ask a question that turns `undecided` into a verdict through
`record_trust`, but it never computes the decision itself. The two choke points
take the verdict as an argument (`trusted: bool`) and do not look the store up
again. That way a decision is made once per run, and a reader can see it in a
single place.

**Fail Early on the store.** A malformed `trust.json` raises, with the path and
the line. Reading a corrupt store as "nothing trusted" would be quiet. Reading
it as "everything trusted" would be worse.

## 4. The store

The store would be `~/.tau/trust.json`, an object that maps a canonical
absolute path to `true` or `false`. The nearest ancestor entry wins. The file
is plain, hand-editable and sorted on write, so a user can manage it with
`$EDITOR` or `jq`, and the proposal does not need a `tau trust` subcommand to
be usable. Since `--approve` decides only one run, persisting a decision takes
either an edit to the file or the TUI prompt.

An alternative was to key trust on the **content** of `.tau/extensions` (a
hash), so that a `git pull` bringing new code into a trusted checkout would ask
again. It loses on τ's threat model: in that checkout the agent already runs
`bash` without asking, and the re-prompt would come on every edit the user
makes to their own extension. It is listed as §6 Q5 because it is a posture
choice.

## 5. Per-head behaviour on `undecided`

This is the only state in which the heads differ, and it is where pi's silent
headless fallback lives.

| Head | Proposed |
|---|---|
| TUI / repl | A modal at startup lists `gated` by path, with four choices: trust this folder, trust the parent, trust this run only, do not trust. Persistent choices go through `record_trust`. |
| `tau -p` | Untrusted for this run. One stderr line names each skipped path and the flag (`-a`) that would load it. |
| `--mode rpc` | The same verdict as `-p`, and `get_state` carries the `TrustDecision`, so a host can see what was skipped without scraping stderr. |
| `tau serve` | The daemon decides per session from its cwd, using `trust.json` only. It never prompts, and a client sees the verdict on the session (§6 Q4). |

Under this rule, `-p` neither prompts nor refuses. Refusing to run until the
user passes `-a` or `-na` would be an error the user did not need, and silently
loading would run code. Skipping with a line that names each skipped path is
not hiding the problem.

## 6. Open questions (for John)

1. **Is the gate on by default?** The recorded stance is that a security
   feature is opt-in, never mandatory and never switched on automatically
   (stated by the owner on 2026-10-02, while designing `tau serve`). The status quo is
   already "project code never runs", so a gate that defaults to `undecided` →
   ask (TUI) / skip (headless) **continues** today's behaviour and only adds a
   way to opt in. The other reading is to have no gate at all: project
   extensions would be off unless `config.json` says
   `"project_extensions": true` or the run says `-a`, with no prompt and no
   store. *What the answer changes:* whether `trust.json` and the TUI modal
   exist at all. Without them the build is about 40 lines and one flag, not a
   module.
2. **Which resources are gated?** The candidates are code (`.tau/extensions`,
   and future skills that carry scripts), τ's own text slot (`.tau/SYSTEM.md`),
   and the ancestor `AGENTS.md`/`CLAUDE.md`. The recommendation is to gate
   **code only**. The text files are instructions the model reads in a session
   that can already run `bash`. Gating `CLAUDE.md` would raise a prompt in every
   repo the first time τ opens it, which is a large change of default, and
   `-nc` already exists for a run that wants none. Gating `SYSTEM.md` while
   leaving `CLAUDE.md` open would be incoherent. *What the answer changes:*
   whether existing users see a prompt on their next run in an ordinary repo.
   Under the recommendation, they would see one only where a
   `.tau/extensions/` exists.
3. **What happens on `undecided` in `-p` and rpc?** Three options: skip with a
   notice (§5, recommended), refuse until `-a`/`-na` is passed, or load.
   *What the answer changes:* whether Tectum and other scripted callers need a
   new flag to keep working in a checkout that grows a `.tau/extensions/`.
4. **Who decides for `tau serve`?** The daemon has no terminal, and a client
   may be on another machine. The options are: `trust.json` on the daemon's
   host only (recommended); or a serve verb that lets a client record trust. A
   client can already make the agent run `bash` there, so the verb adds no new
   power, but it is a remote write to the daemon's `~/.tau`. *What the answer
   changes:* whether `serve/protocol.py` grows a verb and a schema entry.
5. **Is trust keyed by path or by content?** §4 recommends path. *What the
   answer changes:* whether a `git pull` in a trusted checkout can bring in
   code that runs without a new question.
6. **Should `Settings.load` and the `{cwd}/.tau/settings.json` it reads be
   deleted?** It has no caller (§1). The other options are to wire it in or to
   gate it. Deleting it changes an exported, `@agent_facing` name, which is
   allowed below 1.0. *What the answer changes:* whether `settings.json` is on
   the gated list in Q2.

## 7. What is absent, deliberately

- **No lockfile on `trust.json`.** τ is single-writer by stance, and the file is
  meant to be edited by hand.
- **No extension `project_trust` hook** (pi has one). The composable seam is the
  file: any script can write it. A hook would let one extension decide whether
  other code runs, which is a policy surface no use case has asked for.
- **No "do not trust (this session only)" option.** "This run only" with no
  write already means that.
- **Explicit `-e PATH` is never gated.** Naming a path is the user's decision.
  The gate covers discovery only.
- **The global `~/.tau/extensions` and `~/.tau/AGENTS.md` are never gated.**
  They belong to the user and not to a checkout.
- **Skills and prompt templates are not designed here.** Tier 10 adds their
  directories to `GATED` when it builds them.
