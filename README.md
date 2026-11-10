# τ (tau)

τ is an agent harness: it runs a conversation between you, a language model and tools (read,
write, edit, grep, find, bash), and keeps every session as a tree you can branch, continue and
share. A sans-IO Rust core holds the tree and the step algorithm; Python drives it, and is where
providers (any OpenAI-compatible server, Anthropic, Gemini), tools, hooks, extensions and the
terminal UI live.

**Status: 0.16 is a pre-release.** It is a ground-up rewrite of τ 0.15 (tag `v0.15.0`, hotfixes on
branch `0.15.x`). Below 1.0, interfaces may change between minor versions.

## Install

τ needs Linux and Python 3.12 or later. The package is `ffwf-tau` and installs the `tau` command:

```bash
pip install 'ffwf-tau>=0.16'
```

Until 0.16.0 is on PyPI that finds nothing (plain `ffwf-tau` is 0.15 there); build from a
checkout instead, which needs a Rust toolchain:

```bash
git clone https://github.com/jmccardle/tau && cd tau
python3.12 -m venv .venv
.venv/bin/pip install -U 'pip>=25.1'      # --group needs pip 25.1
.venv/bin/pip install --group dev
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release
source .venv/bin/activate
```

## First run

τ keeps its config and sessions in `$TAU_HOME` (`~/.tau` unless you set it). Name your models in
`$TAU_HOME/config.json`. This one has a local OpenAI-compatible server, at the address where
llama.cpp's `llama-server` listens by default, and `mock`, which echoes you and needs no server:

```json
{
  "default_model": "local:qwen",
  "models": {
    "local": {
      "base_url": "http://localhost:8080/v1",
      "models": { "qwen": { "model": "qwen3" } }
    },
    "mock": { "provider": "mock", "models": { "echo": { "model": "echo" } } }
  }
}
```

```bash
tau config check                  # prints nothing when the config is sound
tau -p -m mock:echo "hello"       # one message, the answer on stdout: "mock: hello"
tau -p "what is in this directory?"   # the same, with the default model and the tools
tau                               # the terminal UI
```

For a hosted model, add `"api_key_env": "NAME_OF_YOUR_KEY_VARIABLE"` to its group: the config
names the variable and never holds the key. A server that cannot be reached is an error that
names it.

## Documentation

- [Guides](https://github.com/jmccardle/tau/blob/master/docs/guide/README.md): the quickstart,
  the terminal UI, the model in ten minutes, configuration, writing an extension, writing a
  frontend, and the wire protocol of `tau serve`.
- [Reference](https://github.com/jmccardle/tau/blob/master/docs/reference/README.md): every
  public name, generated from the docstrings.
- [Design](https://github.com/jmccardle/tau/tree/master/docs/design): the model, its invariants,
  and the decisions behind them.
- [Examples](https://github.com/jmccardle/tau/tree/master/examples): small programs on the
  library, a chat client of `tau serve` in Rust, and larger exploratory extensions.

## Contributing

`scripts/check.sh` is the whole gate, in the order CI runs it. The rules the code is held to
are in [`CLAUDE.md`](https://github.com/jmccardle/tau/blob/master/CLAUDE.md).

## License

MIT. See [LICENSE](https://github.com/jmccardle/tau/blob/master/LICENSE).
