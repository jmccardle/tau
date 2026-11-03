# τ (tau)

An agent harness. A sans-IO Rust core holds the session tree and the step
algorithm. Python drives it, and Python is where providers, tools, hooks,
extensions and frontends live.

τ 0.16 is codename **Trusty**: **T**au, **Rust**, and you can still see the tail
of **Py** sticking out. It is a ground-up rewrite of 0.15, which is tag
`v0.15.0` and is maintained on the `0.15.x` branch.

Status: **0.16 in development.** What exists now:

- the library: sessions, cursors, hooks, extensions, providers (OpenAI-compatible,
  Anthropic, Gemini) and the base tools. `examples/chat.py` is a small frontend;
- the `tau` command: `tau -p MESSAGE` (send one message, print the answer, exit with a status
  that says how it ended; `--json` for the events, `--answer` for questions, `--verbose`,
  `--version`), `config`, `ext`, `sessions`, `export`, `context`, `trust`, and `serve`, which
  shares a session over a Unix socket with any number of clients (`tau -p` attaches to it);
- a wire protocol and `RemoteClient` for those clients;
- the terminal UI: `tau` opens it (Textual, `tau.tui`) on a new, continued or attached session:
  the transcript with thinking and tool boxes, live text, the editor, `/` commands with a popup,
  Tab completion and forms for missing arguments, questions as boxes, themes; a cursor bar and
  switching between cursors, Ctrl+Z to edit the last message, the tree screen (go to a node,
  open a cursor there, label), the session picker (`tau -r`, Ctrl+O) and Ctrl+N for a new session.
  The model and config forms follow (Phase 8, 8C-3).

Start with [`docs/guide/`](docs/guide/README.md): a quickstart, the model, and how to
write an extension or a frontend.

- [`docs/design/`](docs/design/): the model, its invariants and the decisions behind them.
- [`docs/plan.md`](docs/plan.md): the phases and where we are.
- [`CLAUDE.md`](CLAUDE.md): the rules and commands.
