# τ (tau)

An agent harness. A sans-IO Rust core holds the session tree and the step
algorithm. Python drives it, and Python is where providers, tools, hooks,
extensions and frontends live.

τ 0.16 is codename **Trusty**: **T**au, **Rust**, and you can still see the tail
of **Py** sticking out. It is a ground-up rewrite of 0.15, which is tag
`v0.15.0` and is maintained on the `0.15.x` branch.

Status: **0.16 in development.** The library runs agents: see `examples/`, for
example `examples/chat.py`. The `tau` command currently offers only
`config check` and `trust`. The frontends (the TUI, `tau serve`) are not built
yet.

- [`docs/design/`](docs/design/): the model, its invariants and the decisions behind them.
- [`docs/plan.md`](docs/plan.md): the phases and where we are.
- [`CLAUDE.md`](CLAUDE.md): the rules and commands.
