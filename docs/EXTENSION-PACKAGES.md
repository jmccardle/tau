# Extensions from installed packages, under one authorization gate

Decision record (2026-10-18), nothing built. τ will load extensions from
installed Python packages through entry points. An extension's identifier
becomes its Python module name, and nothing loads, from a package or from
`~/.tau/extensions`, until the user has authorized it. Tracked as #35, the
headline of 0.14.0. tau-search and tau-router are the two extensions it ships
for.

## 1. What is wrong at 0.13.0

- **An installed package cannot be an extension.** Nothing reads
  `importlib.metadata` entry points. The loader takes `-e <path>` and
  scans one directory (`_discover_extension_paths`,
  `tau-agent-core/src/tau_agent_core/sdk.py:436`). tau-router shipped shim
  files and an install command to work around this.
- **Finding is loading.** Every `*.py` file and package directory in
  `~/.tau/extensions` loads at startup (`sdk.py:646`), with no record of the
  user agreeing to it.
- **One extension has three spellings.** The config key and the topic emitter
  are the file stem (`ext:<stem>:<topic>`, `extension_types.py:52`, :69). The
  command owner is `extension_owner()` (`commands.py:122`), which maps
  everything outside `\w` to `_` and, for an inline factory, keeps the
  qualname's last segment. tau-router's `tau_router.router:register` and
  `tau_router.planner:register` therefore both own commands as `register` (#40).
- **The name parsers assume a name has no `.`.** `--ext-config` splits
  `NAME.KEY` on the first `.` (`tau-coding-agent/src/tau_coding_agent/headless.py:261`),
  so `tau_router.router.profile=p` names extension `tau_router` and key
  `router.profile`. `split_qualified_command` splits `ext:<owner>.<cmd>` on the
  last `.` (`commands.py:182`).
- **Drop-ins load under synthetic module names** (`_tau_ext_<stem>_<n>`,
  `sdk.py:545`), from their file path. Loaded that way, an installed package
  gets a second copy of its module state and loses its relative imports.

## 2. The identifier is the Python module name

Decided by John on 2026-10-18 (#35, decision 2). The module name is the
config key, the topic emitter, the command owner, the `/extensions` label,
what `-e` accepts, and what an authorization record names.

- An entry point's target is a **module** exposing `register(api)`. It is
  loaded with `importlib.import_module` under its real name. A module target
  keeps the loader's existing module-level reads (`CONFIG_SCHEMA`,
  `TOUCHES_BUS`, `SUBJECTS`), and a callable target would need a second
  convention for them. This position came from tau-search and tau-router, and
  the record adopts it.
- A drop-in's module name is its file stem, or its package directory's name.
  So no drop-in's identifier changes (proposal; inferred from the rule, not
  stated by John).
- An extension loaded by explicit path (`-e /abs/dir/handset_bus.py`) is
  identified the same way: `handset_bus`. τ does not derive a dotted name from
  the path's directories. Which directory is the package root depends on
  `sys.path`, and the path doesn't record it, so any derivation would be a
  guess (proposal). A host that wants the dotted name ships the module as an
  entry point and names it with `-e <module.name>`.
- **Alternative rejected:** the entry-point name as the identifier. Two
  distributions can declare the same entry-point name, while a module name is
  unique at import time. That is the property an authorization record needs.

## 3. Colons delimit structure; dots belong to module names

Decided 2026-10-18. Every site that composes or parses a name puts the module
name in whole, dots included, and uses `:` as the delimiter. A Python module
name cannot contain `:`.

| Site | 0.13.0 | 0.14.0 |
|---|---|---|
| `--ext-config` | `planner.profile=p`, split on first `.` | `tau_router.planner:profile=p`, split on first `:` |
| Pub/sub topic | `ext:planner:need_router` | `ext:tau_router.planner:need_router` |
| Qualified command | `ext:planner.budget`, split on last `.` | `ext:tau_router.planner:budget`, split on last `:` |
| Config key | `extensions.planner` | `extensions."tau_router.planner"` |

**Alternative rejected:** mapping `.` to `_` at the parsing sites. The mapping
is lossy, because a module literally named `tau_router_planner` collides with
`tau_router.planner`. It also creates a third spelling of each identity, which
is the #40 defect. `extension_owner()`'s mangling is removed, not extended.

**The migration breaks hard-coded topics silently.** A subscriber still
listening on `ext:planner:need_router` receives nothing, and tau-router's
planner would just report "router not loaded". The companion rule is #44: a
subscription to a topic no loaded extension emits shows in `tau ext list` and
as a debug notice. tau-router has said it will change its spelling in the same
change.

## 4. Typed shorthand resolves, and is never stored

Decided 2026-10-18. At the interactive edge, a user may type the last dotted
segment (`planner`) wherever an extension name is typed: `-e`, `/extensions`,
a qualified command.

- It is resolved fresh on every use, against the **enabled** extensions, with
  no cache. A new install can't silently re-point `planner`.
- If two enabled extensions share the segment, the input is refused, naming
  both full module names.
- Stored config, authorization records and wire output always carry the full
  module name.

## 5. One authorization gate for packages and drop-ins

Decided 2026-10-18 (#35 decisions 1, 3 and 8).

- **Finding is not loading.** Packages (entry points in the provisional group
  `tau.extensions`) and drop-ins are both discovered automatically. Listing
  reads metadata and paths only and imports nothing.
- **Loading needs an authorization record.**
  - For a package, the record names the distribution and version, and a hash
    where one is available. A wheel's `RECORD` holds a sha256 per installed file.
    An editable install's `RECORD` covers only the finder, so it needs a hash over
    the source files, or a record keyed to the path. `direct_url.json` (PEP 610)
    says which kind of install it is. A new version requires authorizing again.
    "Same distribution, new version" is an update and "distribution never seen"
    is new, and the prompt shows which.
  - For a drop-in, the record is keyed by path, with no version.
- **A collision neither blocks loading nor gets past authorization.** A
  same-named extension from an unauthorized source is offered to the user. It
  is never loaded silently, and it never stops τ.
- **Existing drop-ins are grandfathered at upgrade**, so no 0.13.0 setup breaks.
  Proposal for the mechanism: on the first 0.14.0 start, write a path record
  for every drop-in present, and announce it once.
- **Accepted hole:** a file planted in `~/.tau/extensions` before the upgrade is
  grandfathered. John accepted this, because anyone who can write that directory
  already owns the user. For the same reason, editing an authorized drop-in
  does not ask again; path records track no content (inferred consequence).

**Alternatives rejected:**
- tau-router's "placing the file is consent", which keeps drop-ins outside the
  gate. It has real UX merit, but the Tier 8 trust gate (#3) wants the split
  between finding and loading, and John prefers escalating grants.
- tau-search's opt-in drop-ins announced by a loud notice. It breaks every
  existing drop-in setup at upgrade, and grandfathering avoids that.

## 6. Drop-in formats: `.py` files and package directories only

Decided 2026-10-18. Wheels and zips as drop-ins would need unpacking, cleanup
semantics and their own trust story, which makes them a package manager (#13).
A wheel is installed with pip and declares an entry point. Drop-in means
source the user placed; installed means a package the user authorized.

## 7. Where this reaches

- `tau ext list` is one core function in `tau_agent_core` returning records
  (identifier, source kind, distribution and version, target, state, and the
  reason when not loaded). The CLI renders it with no session and no model, the
  TUI's `/extensions` renders it with found-but-not-loaded entries, and
  serve/RPC exposes it read-only. tau-code needs found-but-not-loaded entries so
  it can offer to enable them (#35, tau-code's comment).
- #42: the TUI creates its session at mount, so the startup screen's hint and
  completion read the loaded extension commands. With this gate, mount loads
  exactly the authorized set. A session at mount must leave nothing on disk
  when the user quits without acting.
- #43: `--tools` must check names that aren't built-ins after extensions load.
- #38 (embedding serve with an explicit extension list) is the programmatic
  half of the same question.

## 8. What is absent

- **A package manager** (installing, upgrading, removing). That is #13. τ
  authorizes what pip installed and never installs anything itself.
- **Checking PyPI for updates.** Filed separately as #41.
- **A content hash for drop-ins.** John chose path records; §5 states what that
  leaves open.
- **The final entry-point group name.** `tau.extensions` is provisional until
  the build.
- **The authorization store's format and location**, and the prompt's text in
  each head. These are build decisions, to be recorded here when made.
