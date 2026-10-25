# τ next — "Trusty"

**T**au, **Rust**, and you can still see the tail of **Py** sticking out.

A ground-up rewrite of the [τ](https://dev.ffwf.net/john/tau) agent harness: a
sans-IO Rust core holding the session tree and the step algorithm, driven from
Python, where providers, tools, hooks, extensions, and frontends live. It will
ship as τ 0.16.0.

Status: **Phase 0, scaffold.** Nothing here runs an agent yet. See
[`docs/plan.md`](docs/plan.md) for the model, the invariants, and the phases;
[`CLAUDE.md`](CLAUDE.md) for the rules and commands.
