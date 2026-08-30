# ffwf-tau

The guessable name for **Tau**, a programmable coding-agent harness with an
optional TUI.

```bash
pip install ffwf-tau
tau
```

This distribution contains no code. It depends on the other Tau distributions at
the same version, with the extras that make the program complete on its own, so
the two commands above install `tau` and start it.

## Why it exists

Tau publishes four distributions, and none of them is called `ffwf-tau`:

| Distribution | Imports as | What it is |
|---|---|---|
| `ffwf-tau-llm` | `tau_llm` | the provider and streaming layer |
| `ffwf-tau-agent-core` | `tau_agent_core` | the agent loop, tools, sessions, extensions |
| `ffwf-tau-coding-agent` | `tau_coding_agent` | the `tau` command and the Textual TUI |
| `ffwf-tau-jmfts` | `tau_jmfts` | the optional JMFTS session store |

`pip install ffwf-tau` used to fail with `could not find a version that
satisfies the requirement`, which reads as "wrong Python" or "wrong index" and
is neither. It also matches the name of a command Tau already installs: the
`ffwf-tau` executable is the prefixed spelling of `tau`, for scripts that need a
name PyPI guarantees. One name now means one thing in both places.

Note the `ffwf-` prefix throughout. `tau`, `tau-llm`, and `tau-ai` on PyPI are
unrelated projects.

## What you get, and what you do not

`ffwf-tau` is the completionist install — everything that works on its own:

| It pulls in | So that |
|---|---|
| `ffwf-tau-coding-agent[tui]` | the CLI, the headless modes, and the Textual interface |
| `ffwf-tau-llm[anthropic,google]` | all three wire protocols, not just `openai-completions` |
| `ffwf-tau-agent-core[images]` | `read` on a screenshot works, bounded to 2000px a side |

The provider SDKs and Pillow are extras on their own distributions, and each
raises an error naming its extra when it is missing. That is a good failure, and
still one the guessable name should not hand you.

It deliberately does **not** pull in `[jmfts]`. The JMFTS session store needs a
running server, so an install that added it by default would answer a question
nobody asked. Install it when you want it:

```bash
pip install 'ffwf-tau-coding-agent[tui,jmfts]'
```

If you want the headless agent without any interface libraries, provider SDKs or
native image wheels, install the real distribution rather than this one:

```bash
pip install ffwf-tau-coding-agent
```

## Everything else

Documentation: <https://ffwfrobotics.github.io/tau/>

Repository: <https://github.com/jmccardle/tau>

MIT © Fight Fire with Fire Robotics, LLC
