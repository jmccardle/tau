# Demo reel: τ 0.15 against τ 0.16

Recorded races between the two versions, side by side in one terminal, plus a headless benchmark
that puts numbers on the same races. Everything is reproducible from this directory and runs
against a fake model server, so no network, key or GPU is involved.

```text
demo/render.sh          ->  demo/out/race-{a,b}-{instant,paced}.{mp4,webm,gif}
demo/race.py bench      ->  demo/results.md
```

## Prerequisites

| need | for | how |
|---|---|---|
| Linux x86_64 | everything scripted here | |
| Python 3.12 | both installs (`PYTHON=` names one) | your package manager, or uv |
| Rust (`cargo`) | building the 0.16 wheel | rustup; the repository pins the toolchain |
| network | `pip install ffwf-tau==0.15.0` | PyPI |
| `tmux`, `ffmpeg` | the split panes, the gif | your package manager |
| `vhs`, `ttyd` | recording | `demo/install-tools.sh` (release binaries into `~/.local/bin`, checksums verified) |
| a Chromium or Chrome | vhs renders through it | your package manager |

## One command

```bash
demo/install-tools.sh && demo/setup.sh && demo/render.sh
```

* `setup.sh` makes two isolated virtualenvs: 0.15 from PyPI (`ffwf-tau==0.15.0`), and 0.16 from a
  release wheel built from this checkout (`maturin build --release`, installed, not editable).
* `render.sh` builds the large session for race B the first time (a few minutes), then records
  four races. `RES=1920x1080 demo/render.sh` changes the size; `demo/render.sh a-paced` records one.
* `race.py bench [--k 5]` takes the timings (about ten minutes, most of it the paced takes).
  Run it on an idle machine: it marks `results.md` DRAFT when the load average exceeds 2.

Everything lives in `$DEMO_WORK`, which defaults to `$TMPDIR/tau-demo-UID`. It is deliberately
outside the checkout: τ looks for a project's `.tau/` in every ancestor of the working directory,
so a work directory under a home that has a real `~/.tau` would read it. Each version gets its own
`HOME` and `TAU_HOME` inside the work directory, and its config points at the fake server. The
real `~/.tau` is never touched. `demo/out/` is git-ignored.

## What is raced

The same fake model, the same script, the same prompt and the same fixture files for both
versions. The server checks that both versions made the identical sequence of requests.

**Race A, a long tool chain.** `tau -p` is given one prompt. The fake model asks for 100
`read` calls, one after another (each on a different one of 300 small files), then answers. Both
versions have `read` with a compatible `path` argument. The clock runs from starting the process
to its exit, so it includes starting τ.

**Race B, reopening a large session.** Each version first runs the same scripted conversation, 200
turns of two `read` calls and an answer each, through `tau -p -c`, building a session of 600 model
samples. The race is `the TUI reopening it in a terminal pane (tmux, 160x48 for the benchmark), timed until the last
turn's text is on screen (*drawn*) and until text typed into the editor is echoed
(*responsive*). The video stops at drawn. 0.16 opens the session with `tau -c`; in 0.15 `-c` is headless only, so the TUI is started with `--resume`, which lists the sessions, and Enter is pressed on the one there is. That picker is part of 0.15's time, as it is part of what a person does.

**Two takes of each.**

* `instant`: the fake model answers at once. The time is τ's own overhead.
* `paced`: the fake model takes 400 ms to its first token and streams 70 tokens/s (a token is four
  characters), the same for both versions. In A the model's time dominates, so the versions finish
  close together; that is the honest picture of what a user feels. In B, after the reopen, one more
  message is sent and the reply streams in while the TUI redraws, so the video shows the TUI
  while it streams. Tune it with `fake_openai.py --ttft-ms --tokens-per-sec`.

## What the numbers do and do not say

* They are wall-clock times of whole processes on one machine, median of five runs after a warm-up,
  alternating which version goes first. `results.md` records the CPU, kernel and both versions.
* A fake model has no latency of its own, so *instant* exaggerates what a real session sees; it
  isolates the harness. *Paced* is nearer to use, and tool calls are still cheap, small files.
* Startup is part of race A. `results.md` also gives the startup of one tool-less answer so it
  can be subtracted.
* Race B's end is detected by reading the tmux pane about every 10 ms, so the resolution is about
  that. Both versions are timed the same way, but "drawn" means the last message is visible, not
  that every earlier message has been laid out: a TUI that draws the newest part first and the
  rest later is credited for that, which is what a user sees.
* 0.15 and 0.16 are different programs. The races use only what both have: `tau -p`, `read`, a
  session that can be continued, and the TUI. 0.15's default of no step limit is matched by
  raising 0.16's `step_limit` to 100000; context files are turned off in both (`-nc`), and so is automatic compaction: the fake model picks its reply by how long the conversation is, which a compaction would shorten.
* Not measured: memory, other tools, compaction, real model servers, other platforms.

## Files

| file | what |
|---|---|
| `fake_openai.py` | stdlib-only OpenAI-compatible server that replays a JSON script (SSE and plain, `/v1/models`, `--paced`) |
| `scenario.py` | the fixture files and the scripts |
| `common.py` | work directory, the two sandboxes, configs for each version's format |
| `race.py` | `prepare`, `build-b` and the headless `bench` that writes `results.md` |
| `visual.py` | the tmux split-pane race a tape runs |
| `tapes/` | one vhs tape per race, and the shared settings |
| `render.sh`, `setup.sh`, `install-tools.sh` | the pipeline |

Only one of `race.py`, `visual.py` and `render.sh` may run at a time: they share the two sandboxes.
