# The public API

Three kinds of reader use tau as a library, and the public surface is what one of them needs:

- **An extension author** writes a module of hooks, tools, commands and options.
- **A frontend author** writes a program that shows a session and takes input, in this process or over `tau serve`.
- **An embedder** runs sessions from their own program, with their own providers, or runs the step machine with their own loop.

Everything else is private. `from tau import Name` reaches every public name; the only other
public modules are `tau.cli` (`main`, the flags), `tau.commandline` (the pure line parser) and
`tau.trace` (profiling). Every other module's name starts with an underscore, and
`scripts/api_docs.py --check` fails when one that does not has nothing listed in
[`groups.toml`](groups.toml).

Three files keep this honest, and CI checks them:

- [`groups.toml`](groups.toml) says what is public and in which group (hand-written).
- [`surface.txt`](surface.txt) lists every public name with its signature, sorted, so any change
  to the API is a readable diff in review.
- [`../reference/`](../reference/README.md) has one page per group, from the docstrings.

A name is public when it is in `tau.__all__` and in a group. A class that tau makes and hands
over (a handle, a client from `Driver.client` or `RemoteClient.connect`) is listed under
`made_by_tau` in `groups.toml`; its constructor is not part of the surface. A class lists no
member that a public base class already declares: it is documented there, once. A public
signature may name only tau's public types, the standard library's, and the packages on the
`foreign_types` list of `groups.toml` (`httpx`, for the providers' `transport`); the check fails
otherwise.

## Writing an extension

The `Extension` contract and its hooks, the handles a hook is given (`Cursor`, `NodeHandle`,
`TreeHandle`, `Subject`), `CallDraft`, command and option `Form`s, and the named types a
parameter may use. For an extension author and for anyone who reads what an extension is.

## Tools

`Tool`, the `ToolError` a tool raises, `Progress`, and the four base tools. An extension author
writes tools, and an embedder hands them to a driver.

## The session's records

`Node`, `Body`, `Part`, `Outcome`, `Author`, `Stop` and the other values a session is made of.
Every kind of reader reads them, so they are one page.

## Writing a frontend

`Client` and its two implementations, the command line a person types (`Parsed`,
`UnknownCommand`, `commandline`), and `CommandFailed`. For a frontend author.

## Events

What a `Client`'s stream yields. For a frontend author.

## Starting and finding sessions

Starting a session the way `tau` does, listing and reopening others, exporting one, and the
argparse flags that go with it. For a frontend author, before there is a client.

## The wire

`PROTOCOL`, `Welcome`, the errors of a bad handshake, and `Server`. For a frontend author in
another language, who reads the generated schema instead, and for a Python program that serves.

## Embedding tau

`Driver`, the clock it reads, and the state a test can read to tell quiet from busy. For an
embedder.

## Providers

The `Provider` contract and what it returns, and the three hosted providers (`OpenAIProvider`,
`AnthropicProvider`, `GeminiProvider`). They are public because an embedder builds one with its
own `transport` (an `httpx` transport) or `environ`, and because someone writing a fourth
provider starts from them. The scripted mock provider is not a name: `builtin_providers()["mock"]`
gives it.

## The step machine

`Session` and the jobs it hands out. For an embedder whose program is its own driver: the core
does no IO, and this is the only way to run it without `Driver`.

## Configuration and context

Reading the config files, trust, extension loading, the system prompt. For a frontend author and
an embedder who start sessions without the `tau` program.

## Errors

What a session and its store raise. Every reader catches some of them.

## Tracing

`tau.trace`: one timeline across Rust and Python. For an embedder looking for where time goes.

## Decided in Phase 7C-1

- `effects`, `events`, `handles`, `protocols`, `clock`, `client`, `remote`, `driver`, `sessions`,
  `session_start`, `step_limit`, `base_tools`, `context`, `markdown` and `providers` were
  modules whose contents `tau` already re-exported. They are now `_effects`, `_events` and so
  on: there is one way to import a name.
- `commandline` stays public: a frontend reads a typed answer with `value` and `values`, and
  the tests that cover the parser need it. `Parsed` and `UnknownCommand` are exported because
  `Client.parse_line` returns and raises them.
- The three hosted provider classes are exported (see Providers above); `MockProvider` is not.
- Names that the signatures of public things showed but `tau` did not export are now exported
  (`Json`, `Layer`, `Origin`, `Severity`, `SourceKind`, `SourceState`, `Ask`, `Pick`,
  `DriverState`, `CURSOR_POINTS`, `TOOL_POINTS`) or are gone from the signature.
- Review removed `MockProvider` and `safe_block` from `tau.__all__`. `Trusted` and `LayerText`
  stay because public signatures name them (`LoadedConfig.project`, `Layer`,
  `UserConfig.project`, `UntrustedProject`). Every other name in `tau.__all__` is one a reader's
  signature, a raise or a test needs.
- `RemoteClient` is `made_by_tau`: `RemoteClient.connect` builds one, so its constructor is not
  part of the surface. `LocalClient` and `RemoteClient` subclass `Client`, and `Client` documents
  every call once.
