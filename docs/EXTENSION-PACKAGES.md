# Extensions from installed packages, under one authorization gate

Built 2026-10-18 on branch `feat/ext-packages`, not yet merged; §9 holds the
build's proposals, which await John. τ loads extensions from installed Python
packages through entry points. An extension's identifier is its Python module
name (a file's stem, verbatim), and nothing discovered loads, from a package or
from `~/.tau/extensions`, until the user has authorized it. Tracked as #35, the
headline of 0.14.0, with #40, #43, #44 and #47 on the same branch (§10–§12).
tau-search and tau-router are the two extensions it ships for.

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
### 2.1 A file-loaded extension: its identifier and its import name differ

A drop-in, or a file named with `-e PATH`, has no module name of its own.
`my-ext.py` is a legal filename, but `my-ext` is not a Python identifier, so it
cannot be imported as `my-ext`. Python already handles this case by keeping
two names apart (decided by John on 2026-10-18, #35 decision 10; the precedents
are below):

- **The identifier is the stem, verbatim.** For `my-ext.py` it is `my-ext`, and
  for a package directory it is the directory's name. τ uses it as config key,
  topic emitter, command owner, `/extensions` label and authorization name. It
  is not mapped to `my_ext`, because John rejected lossy mapping in §3: `my-ext`
  and `my_ext` are two files and stay two identities.
- **The import name is private.** τ loads the file with
  `importlib.util.spec_from_file_location(<private name>, path)` and never
  shows that name. This is what 0.13.0 does already (`_tau_ext_<stem>_<n>`,
  `sdk.py:545`). For a package directory it passes
  `submodule_search_locations` (`sdk.py:546-547`), so `from .helper import x`
  inside the directory still works. It is also the recipe in the importlib docs, "Importing a
  source file directly", where the caller chooses the module name.
- **Precedents.** A distribution named `my-ext` that imports as `my_ext` keeps
  its public name and its import name apart in the same way. pytest's
  `--import-mode=importlib` and `runpy.run_path` also run files under names
  they choose themselves.
- **Only installed packages are imported under their real name**, with
  `importlib.import_module` (§2). A private name there would split module state
  and break relative imports, which is tau-search's finding. A file-loaded
  extension has no real name for code to import, so a private name costs it
  nothing.

The stem's characters matter only where τ parses a delimiter. So a legal
filename is never an error at startup:

| Stem contains | Effect |
|---|---|
| `-`, digits first, a keyword (`class.py`), non-ASCII | Nothing. The identifier is a string, and only the import name has to be an identifier. |
| `.` (`my.ext.py`) | The identifier `my.ext` reads as dotted. It is unambiguous, because authorization names the source (§5). The shorthand segment is `ext`. |
| `:` or whitespace (both legal on Linux) | `:` would break `--ext-config` and topic parsing, and whitespace would break commands, which `qualified_command` already refuses (`commands.py:148`). **Decided by John on 2026-10-18:** `tau ext list` shows the file as found but unusable, naming the character and saying to rename it. The file does not load. τ keeps running (decision 1), and the problem is shown rather than hidden. |

An extension loaded by explicit path (`-e /abs/dir/handset_bus.py`) follows
the same rule, so its identifier is `handset_bus`. τ does not derive a dotted
name from the path's directories. Which directory is the package root depends
on `sys.path`, and the path doesn't record it, so any derivation would be a
guess (decision 10). A host that wants the dotted name ships the module as an
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
- **An extension named on the command line is authorized by being named.**
  This covers `-e PATH` and `-e <module.name>`. Decided by John on 2026-10-18,
  for headless hosts with no TTY: Tectum spawns `tau --mode rpc
  --no-extensions --extension PATH`, and #38's embedders do the same. The gate
  covers what τ *discovers*, and `--no-extensions` turns discovery off.
- **A collision neither blocks loading nor gets past authorization.** A
  same-named extension from an unauthorized source is offered to the user. It
  is never loaded silently, and it never stops τ.
- **Existing drop-ins are grandfathered at upgrade**, so no 0.13.0 setup breaks.
  The mechanism, as built (§9): on the first 0.14.0 start, write a path record
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

## 8. How it was built

- **Identity** is one function, `extension_sources.extension_identifier(label)`,
  over the runner bucket's label: a file path gives the stem verbatim, a package
  directory its name, an installed module its name, and an inline SDK factory
  its `module.qualname`. `ExtensionHandlers.name` reads it, and so do the config
  key, the topic emitter, `ExtensionAPI.extension_name`, `/extensions` and a
  lock's display name. `extension_owner()` is deleted (#40).
- **An inline factory is `module.qualname`**, not its last segment, so two
  factories both named `register` no longer collide (the third case of #40).
  This is a build decision that follows decision 2; the shorthand (§4) lets a
  user still type the last segment.
- **Planning is separate from importing.** `sdk.plan_extensions` decides from
  metadata, paths and the store what loads and what is withheld, and writes
  nothing except the grandfathering store. `_load_extensions` imports the plan.
  `tau ext list` calls the planner with `write=False`.
- **Load order and collisions.** Discovered drop-ins load first, then
  discovered packages, then `-e` values in argument order. An `-e` value
  claims its identifier before discovery runs. A discovered source whose
  identifier an earlier one claimed is withheld as `collision`; τ keeps running
  (decision 1). Two routes to one file load it once, as explicit.
- **`-e VALUE`** is a path when it holds a `/` or ends in `.py`. Anything else
  is tried as an entry point's module, then as the last dotted segment of one
  (refusing on ambiguity, naming both), then as any importable module
  (`kind="module"`).
- **Explicit failures** raise `ExtensionStartupError` naming the extension and
  phase, with the original exception as `__cause__` (§12).
- **The listing** is `sdk.extension_records` (no session) and
  `AgentSession.extension_records` (live), both returning
  `extension_sources.ExtensionRecord`. The CLI (`tau ext list`), the TUI's
  `/extensions`, the REPL and the RPC/serve verb `list_extensions` render them.
  States: `loaded`, `disabled`, `failed`, `authorized` (would load; no-session
  listing only), `unauthorized`, `unusable`, `collision`.
- **Wire versions: RPC 3.0, serve 0.11.** The qualified-command spelling
  changed from `ext:<id>.<cmd>` to `ext:<id>:<cmd>` inside existing string
  fields (`get_commands`, the attach surface). By the RPC rule
  (`rpc/protocol_doc.py`: a field's meaning changed is a MAJOR bump), that
  breaks a client that parses the names, so RPC goes 2.2 → **3.0**. The additive
  `list_extensions` rides the same bump. The build first called the names
  opaque and bumped only to 2.3. John corrected that on 2026-10-18: "I don't
  mind seeing the RPC number go up, let's just stay consistent with tracking
  the RPC protocol as we release." Serve is below 1.0, bumps its minor for
  every change (breaking ones included, as 0.9 did for the `enabled`→`auto`
  rename), and refuses any mismatch, so it goes 0.10 → 0.11. tau-code must
  match 0.11, and an RPC host that gates on MAJOR (Tectum gates on 2) must
  accept 3.

## 9. Build proposals, for John

Each item works as described now, and each is a proposal that John may change.

- **Store location:** `<tau home>/extension-authorizations.json`, beside the
  extensions directory it governs (`store_path_for`). A test or host that points
  the drop-in directory elsewhere gets its own store beside it.
- **Store format:** JSON, `{"format": 1, "packages": {...}, "dropins": {...}}`.
  `packages` is keyed by module name, each with `distribution`, `version`,
  `digest` (`sha256:` over the RECORD lines of the module's top-level package,
  or `null`), `editable_path` (from PEP 610 `direct_url.json`, or `null`) and
  `authorized_at`. `dropins` is keyed by resolved path, each with `identifier`,
  `authorized_at` and `grandfathered: true` when written at upgrade. A record
  matches only when distribution, version, digest and editable path are all
  equal. A changed one is an `update` that names the previous version. A store
  that fails to parse, or has another `format`, stops startup with "fix or
  delete it". The write is atomic.
- **Grandfathering:** the first discovery that finds no store writes one,
  recording every usable drop-in present, and every head announces it once:
  "authorized N existing drop-in extension(s) at upgrade: a, b. From now on τ
  loads a new drop-in or package only once it is authorized; `tau ext list`
  shows each one's state." Packages are never grandfathered. `--no-extensions`
  writes no store.
- **Entry-point group:** `tau.extensions` is kept as the final name. tau-router
  already ships it.
- **Headless behaviour:** `-p`, `--mode rpc`, `--mode repl` and the serve
  daemon never prompt and never block. Each unauthorized extension is skipped
  with one stderr line (the daemon logs it), and the run goes on: "[τ] extension
  X (dist) was found but not loaded: <reason>. To load it: tau ext authorize X".
- **The non-interactive route:** `tau ext authorize NAME` (an identifier, its
  last segment, or a drop-in path; `--package` picks the package when a drop-in
  shares the name) and `tau ext revoke IDENTIFIER` (full name only, with no
  shorthand, because removing records should not involve a guess).
  `tau ext list [--json] [-e …]` imports nothing and writes nothing.
- **TUI prompt:** one dialog per unauthorized extension after a load. Title:
  "Load the new extension X?" or "Load the updated extension X?". Body: what the
  source is (for a package, the distribution and version, or "was updated from
  A to B since you authorized it"), "An extension runs its own code in every τ
  session that loads it. Authorizing it is remembered until its package version
  changes; `tau ext revoke X` undoes it." The buttons are "Authorize and load",
  which writes the record and loads it into the running session, and "Not now".
  Escape is "Not now". "Not now" is remembered until the app quits, so a new
  chat does not ask again. The text comes from `sdk.authorization_prompt`, so
  another head can show the same words.
- **No authorizing over the wire in 0.14.0.** `list_extensions` is read-only.
  `AgentSession.authorize_extension` exists for the TUI, and putting it on RPC
  waits on deciding who may grant code execution from a remote head.
- **The `--tools` flag is an allowlist over every tool** (§11), so
  `--tools read,bash -e ext.py` no longer offers ext.py's tools. A model
  entry's `"tools"` key keeps its 0.13.0 meaning (built-ins only). The
  alternative is a flag that only checks extension names and filters nothing,
  which would not narrow tau-search's two tools to one.
- ~~**Command names on the wire changed spelling** without a MAJOR bump (§8).~~
  Decided by John on 2026-10-18: RPC 3.0 (§8, "Wire versions").

## 10. #44: a subscription nothing can answer

The issue left one point open: whether "emits" means declared topics or
observed emitters. **The build checks observed emitters.** The topic's middle
segment is its emitter, and `api.emit` always derives that segment from the
emitting extension's own identifier. So a subscription to
`ext:<emitter>:<topic>` whose emitter is not a loaded extension can never be
delivered, and checking that needs no declaration. It catches the case that
motivated the issue: a subscriber still on `ext:planner:need_router` after the
emitter became `tau_router.planner`.

Each bucket records its `ext:` subscriptions, and an unsubscribe removes one.
`AgentSession.orphan_subscriptions()` compares them with the loaded
identifiers. The result is each record's `orphan_topics` in every listing, and
a debug notice after load: the `tau.extensions` logger, the TUI's log, and
`-p`/`--mode rpc` stderr under `--verbose`. It is never an error.

**Not caught:** a loaded emitter that never emits the subscribed topic. That
needs a declaration (an `EMITS` module attribute, like `SUBJECTS`), which
nothing reads yet (§13).

## 11. #43: `--tools` after extensions load

Built-in names in `--tools` are still constructed up front
(`resolve_tool_names` skips other names). Both `--tools` and `--exclude-tools`
now reach `AgentSession` as an allowlist and a denylist over every tool it
offers, extension tools included. The allowlist rides its own config key,
`tool_allowlist`, which only the flag writes. A model entry's own `"tools"`
key in `config.json` still selects built-ins and leaves extension tools
offered, as two existing tests encode (`test_backend_tool_filter.py`,
`test_json_mode_tool_prints.py`). `AgentSession.check_tool_filters` raises
`Unknown tool: <name>` for a name that matches no built-in and no registered
tool, and `load_extensions` calls it once loading is done. `--exclude-tools`
had the mirror defect: a name that was not a built-in was ignored silently. It
now filters extension tools and is checked the same way. A suppression flag
(`--no-tools`, `--no-builtin-tools`) yields no allowlist. The SDK's
`create_agent_session(tools=…)` still takes built-in names only.

## 12. #47: an explicitly named extension that fails stops startup

Decided by John on 2026-10-18 (#47). In `-p` and `--mode rpc`,
`headless.start_extensions` loads and then fires
`emit_session_start(strict=True)`. An extension named with `-e` whose import,
`register()` or `session_start` raises becomes a `CLIError` with a non-zero
exit before the first turn. The message is `extension <id> (<path>), named with
-e, failed at <load|session_start>: <error>`. A discovered extension keeps
warn-and-continue (#29's rule), and its record is `failed` with the reason,
`session_start raised: …` included. The TUI and `--mode repl` keep collecting
`session_start` failures, because a human is present to read them. The REPL
turns an explicit load failure into a `CLIError` instead of a traceback.

## 13. What is absent

- **A package manager** (installing, upgrading, removing). That is #13. τ
  authorizes what pip installed and never installs anything itself.
- **Checking PyPI for updates.** Filed separately as #41.
- **A content hash for drop-ins.** John chose path records; §5 states what that
  leaves open.
- **Authorizing over RPC/serve** (§9).
- **Declared topics (`EMITS`)** for #44's topic-level check (§10).
- **Reloading a package in place across a version change.** `reload_extension`
  on a package calls `importlib.reload` on its module, which does not re-import
  the package's other modules.
