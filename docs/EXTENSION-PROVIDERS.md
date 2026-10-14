# Providers from extensions, and a package manager (Tier 11 M4/M5)

**§2 built 2026-10-12; §3 (an extension-API `register_provider`) and §4 (a
package manager) are a research record (2026-10-12), nothing built.** The
library half of M4 has existed since 0.9.3. What blocks the extension half is
four decisions, listed in §5. They belong to the owner, and one of them reverses
a written decision.

## 1. What was wrong, measured

**The ROADMAP's "zero `registerProvider` symbol" is stale.** `tau_llm.providers`
has exported `register_api`, `register_provider`, `ProviderSpec`,
`get_provider_spec` and their `unregister_*` pairs since 0.9.3 (`51db776`). Both
registries are process-global dicts in `providers/base.py`, both refuse a
duplicate without `replace=True`, and `tau_llm.client._resolve_request` reads
them on every call. A data-only vendor is one `register_provider` call
(`docs/tau-llm.md`, "Extending"). What does not exist is the extension surface:
there is no `ExtensionAPI.register_provider` and no lifetime tied to an
extension.

**An extension that calls the library directly works once per process.**
Measured (ran) with a probe extension that calls `register_provider(groq)` at
import time, loaded into two `create_agent_session` sessions in one process:

```
session 0 loaded 1 errors []
session 1 loaded 0 errors ["provider 'groq' is already registered; pass replace=True to override it deliberately"]
```

The loader re-executes the module on every load: `sdk._load_one_extension` uses
`spec_from_file_location`, which builds a fresh module object. Several things
load the same extension again in one process. The TUI does it on every new
chat, model swap and resume (`app.py` `_load_backend_extensions`), `reload_extension`
does it, and `tau serve` loads extensions once per hosted session, concurrently
(`serve/daemon.py` `SessionHost.start`). Nothing unregisters a provider either.
`AgentSession._unregister_bucket` unwinds tools, commands and shortcuts, and
providers are not among them.

**Every head builds the startup model before extensions load.** `tau -p` calls
`create_backend` (`headless.py`, which builds the `Model`) before
`backend.load_extensions`. `tau --mode rpc` and `tau serve` (`_host_for` →
`SessionHost.start`) use the same order. The TUI's `action_new_chat` creates the
backend before `_load_backend_extensions`. So a config model whose `backend`
names a vendor that an extension registers was built with no spec in the
registry. When the entry gave no `base_url`, `build_model_from_config` filled
in OpenAI's, which made the misroute below possible.

## 2. Built: no endpoint is an error, never OpenAI's

That fill-in sent a vendor's credential to OpenAI. The `Model` was built with
`base_url="https://api.openai.com/v1"`. The extension registered `groq` later.
At request time `_resolve_request` preferred the model's `base_url` and took the
key from the now-registered spec. Measured (ran):

```
https://api.openai.com/v1 gsk-probe
```

That is Groq's key sent to OpenAI's endpoint, with no error. `register_provider`'s
own docstring names this failure ("a silently redirected vendor sends prompts and
credentials somewhere the operator did not choose"). The ordering in §1 made it
reachable from configuration alone.

`build_model_from_config` now raises when the entry has no `base_url` and the
`backend` is not a registered vendor with a default endpoint. The message names
the registered vendors. `tau_agent_core.sdk.resolve_model` had the same
fill-in and now raises the same way. For a non-built-in id it also takes
`provider`'s registered `api` and endpoint, where before it hardcoded
`openai-completions` and OpenAI's URL. Before the change,
`resolve_model("x", provider="anthropic")` built a model that `_resolve_request`
then refused for an api mismatch. A third defect in the same function, found
while there: a `base_url` override assigned onto the shared `_DEFAULT_MODELS`
entry, so every later `resolve_model("gpt-4o")` in the process inherited it. It
now returns a `model_copy`.

**Why raise rather than resolve late.** Leaving `Model.base_url` unset so the
client resolves it at request time would have made the startup case *work*.
But `Model.base_url` is a required `str`, and `compat.detect_compat`, the TUI's
endpoint row and the pool key all read it as one. Making it optional is a
change to `tau-llm`'s core type for a case nobody has asked for yet.

**What it changes.** A config entry with an unregistered `backend` and no
`base_url` now refuses to build. Before, it built and pointed at OpenAI. No
test or example in the repo used that shape. `ANTHROPIC-GOOGLE-CLIENTS.md` §5's
"then the historical default" still describes `api`, which keeps its
`openai-completions` default for an unregistered vendor that does state a
`base_url` (a self-hosted server). It no longer describes `base_url`.

**What it leaves.** A startup model that names an extension-registered vendor
now fails at startup with a sentence instead of misrouting. A `/model` switch
or `ctx.set_model` made after extensions load resolves through
`ConfigModelResolver` at that moment, so it finds the spec. That asymmetry is
§5 question 4.

Covered by `tau-coding-agent/tests/test_config_names_a_wire.py`
(`TestNoEndpointIsNotOpenAIs`) and `tau-agent-core/tests/test_agent_session.py`
(`TestSDKHelpers`).

## 3. The extension surface (M4): its shape and why it is not built

The obvious shape is `api.register_provider(spec)` and
`api.register_api(id, factory)`. The extension's runner bucket
(`ExtensionHandlers`) records each one beside `tools`/`commands`/`shortcuts`,
and `_unregister_bucket` unwinds them on disable, reload and shutdown. The code
is small, about 80 lines plus tests. Four things stop it from being a decision
this pass could make alone:

1. **It reverses a written decision.** `EXTENSIONS-DEMO-ROADMAP.md` §9 lists
   `registerProvider` as a non-goal: "provider work is tau-llm's, not an
   extension's". The ROADMAP repeats this as "deferred by written decision".
2. **The scopes do not match.** The registries are process-global. Extension
   instances are per session, and one process holds several sessions (§1). A
   bucket that unregisters on teardown takes the vendor away from every other
   live session in that process. The candidate rules are:
   - **(a) Refcount equal specs.** `ProviderSpec` is a frozen dataclass, so `==`
     is field equality. A second equal registration raises the count and an
     unequal one raises an error. Teardown lowers the count, and the entry is
     removed at zero. Cheap. It does not cover `register_api`, because a
     reloaded module's factory is a new function object and never compares
     equal.
   - **(b) A per-session registry overlay** that `_resolve_request` consults
     first. Correct, but every `stream_simple` caller would have to thread a
     registry through, and that seam is `tau-llm`'s public surface.
   - **(c) No extension registration.** Providers stay process-scoped and are
     registered before any session exists (see 3).
3. **Ordering.** Under (a) or (b) the startup model is still built before the
   vendor exists (§1). The fixes are re-resolving the startup model after
   extensions load (three heads, plus serve), or declaring providers somewhere
   that is read before any model is built:
   - a `providers` section in `~/.tau/config.json` that each head registers at
     config load;
   - a module attribute (`PROVIDERS = [...]`) read by a pre-pass;
   - a Python entry-point group.
   The config section covers the common case with no code at all. That is the
   data-only OpenAI-compatible vendor, which `tau-llm.md` estimates at roughly
   25 of pi's 39 providers.
4. **Models.** pi's `registerProvider` carries a model list, and those models
   appear in pi's picker. τ's model catalog is config `models`, read by
   `ConfigModelResolver`, which RPC `get_models`, `set_model` and the TUI picker
   all go through. Letting an extension add models gives that catalog a second
   source in every head. Without it, an extension's vendor is reachable only
   through a `models` entry the user writes by hand.

**Recommendation, not built.** Answer item 1 with (c) plus a config
`providers` section: data-only vendors need no Python, and there is no ordering
problem and no ownership problem. A new wire protocol (`register_api`) stays
Python, through a `tau.providers` entry-point group loaded at startup. Build
(a) only if an extension that has to register a vendor at runtime turns up.

## 4. A package manager (M5): cost record

**What τ has.** An extension is one `.py` file, found in `~/.tau/extensions` or
passed with `-e`. τ has no package concept, no manifest and no dependency
install, so an extension that imports a third-party library needs that library
already in τ's interpreter. `LoadedExtension.content_hash` (a sha256 of the
exact bytes compiled) already exists and is the obvious pin.

**What pi has, for scale.** `package-manager.ts` is 2683 lines. It installs
from `npm:`, `git:` and local sources at global or project scope, and it pulls
extensions, skills, prompts and themes out of a package manifest
(`pi-manifest.ts`). `trust-manager.ts` and `project-trust.ts` gate it.

**Dependencies on unbuilt work.**
- **The trust gate (Tier 8).** A package manager is the path by which code
  arrives on the machine. It must call the trust gate's approval rather than
  invent its own. The gate is being designed in parallel and has no symbol yet.
  Building M5 first would mean either an ungated installer or a second trust
  model.
- **Tier 10's resource loader.** Skills and templates do not exist yet, so a
  package could carry only extensions and themes today.

**Options, priced (inferred; none prototyped):**

| Option | What τ writes | Size |
|---|---|---|
| **pip/uv is the package manager.** A distribution declares a `tau.extensions` entry-point group, and τ discovers it beside `~/.tau/extensions`. | Entry-point discovery in `_load_extensions`, a source column in `/extensions`, the trust hook | ~150–300 lines + tests |
| **A git-clone manager.** `tau install/update/remove/list` clones into `~/.tau/packages/<name>`, pins a commit in a lockfile, and loads the files the manifest lists. | Clone/update/remove, manifest schema, lockfile, CLI verbs, the trust hook, dependency handling (still unsolved) | ~800–1500 lines + tests |
| Both | Sum, less the shared discovery | — |

The first option suits the owner's stated preference for Unix-style composition: it
reuses the installer people already have, and dependencies come along for free.
Its cost falls on the standalone builds. Their interpreter is frozen, so
"pip install into τ" needs a documented target there. This record has not
checked whether one exists.

## 5. Open questions

1. **Should extensions register providers at all?** Saying yes reverses
   `EXTENSIONS-DEMO-ROADMAP.md` §9. *No* closes M4 as "library half shipped in
   0.9.3, plus §2", leaving only the config section from question 2. *Yes*
   means building §3's bucket-owned registration and answering questions 3 and 4.
2. **Add a `providers` section to `~/.tau/config.json`?** Yes removes the ordering
   problem for data-only vendors in all three heads, with no extension
   involved. It adds a config key, which every head has to register before it
   builds a model.
3. **If question 1 is yes, which ownership rule: refcount-equal-or-raise (§3,
   2a) or a per-session overlay (2b)?** 2a is small and process-global. 2b is
   correct for `tau serve` but changes `tau-llm`'s public call seam.
4. **If question 1 is yes, what happens at startup?** One option keeps §2's
   refusal: a startup model on an extension's vendor is an error, and a later
   `/model` works. The other re-resolves the startup model after extensions load,
   which changes four startup paths (TUI, `-p`, rpc, serve).
5. **May an extension add models to the catalog?** This decides whether
   `get_models` and the picker have one source or two.
6. **What shape should M5 take, and does it wait for Tier 8?** The choice is
   between pip with entry points, a git manager, or both. The recommendation is
   to wait: an installer built before the gate is either ungated or a second
   trust model.
7. **§2 is built and changes behaviour.** A config with an unregistered
   `backend` and no `base_url` now refuses instead of reaching OpenAI. Reverting
   it means reverting one hunk in each of two functions. The recommendation is
   to keep it, because the alternative sends a credential to the wrong vendor.

## 6. What is absent

- **`ExtensionAPI.register_provider` / `register_api`.** These are blocked on
  question 1. An extension that calls `tau_llm.providers.register_provider`
  directly still breaks on its second load in a process (§1). Until question 1
  is answered, the working pattern is to register from code that runs once per
  process: an SDK embedder's own startup, or a `sitecustomize`. Not from an
  extension module.
- **Late base-URL resolution** (`Model.base_url: str | None`). Rejected in §2
  as a core-type change made without demand.
- **Extension-contributed models** (question 5).
- **Any package-manager code** (question 6). Nothing here pre-empts the trust gate.
- **pi's `oauth` and `streamSimple` provider options.** `register_api` with a
  `Provider` subclass already covers `streamSimple`. OAuth needs a credential
  store that τ does not have.
