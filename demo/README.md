# Demo reel: τ 0.15 against τ 0.16

Recorded races between the two versions, side by side in one terminal, plus a headless benchmark
that puts numbers on the same races. Everything is reproducible from this directory and runs
against a fake model server, so no network, key or GPU is involved.

```text
demo/render.sh          ->  demo/out/race-{a,b}-{instant,paced}.{mp4,webm,gif}
demo/race.py bench      ->  demo/results.md
demo/resources.py       ->  demo/resources.md   (CPU and memory of three clients; see Resources)
```

## Prerequisites

| need | for | how |
|---|---|---|
| Linux x86_64 | everything scripted here | |
| Python 3.12 | both installs (`PYTHON=` names one) | your package manager, or uv |
| Rust (`cargo`) | building the 0.16 wheel | rustup; the repository pins the toolchain |
| network | `pip install ffwf-tau==0.15.0` | PyPI |
| `tmux`, `ffmpeg` | the split panes, the gif | your package manager |
| `vhs`, `ttyd` | recording | `demo/install-tools.sh` (pinned release binaries into `~/.local/bin`, SHA256 pinned in the script) |
| a Chromium or Chrome | vhs renders through it | your package manager (`install-tools.sh` checks) |

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
  `results.md` is git-ignored: a result belongs to one machine and one run.

Everything lives in `$DEMO_WORK`, which defaults to `$TMPDIR/tau-demo-UID`. It is deliberately
outside the checkout: τ looks for a project's `.tau/` in every ancestor of the working directory,
so a work directory under a home that has a real `~/.tau` would read it. Each version gets its own
`HOME` and `TAU_HOME` inside the work directory, and its config points at the fake server. The
real `~/.tau` is never touched. `demo/out/` is git-ignored.

## What is raced

The same fake model, the same script, the same prompt and the same fixture files for both
versions. The fake server hashes what each version sends it, and `race.py bench` compares the
hashes. Each request is normalised (trailing whitespace dropped, content parts joined, tool-call
arguments re-serialised with sorted keys, tool-call ids ignored) and four views are hashed:
the dialogue with its tool calls (role, text, tool name and arguments), the tool results, the
system prompt, and the tool schemas. The bench **fails** if the dialogue and tool calls differ
between the versions or between runs of one version, if the final answer of race A or of the
built session lacks its marker text, or if a client does not stream. Tool results, system prompt
and tool schemas are each version's own, so `results.md` reports whether they matched rather than
requiring it. This is done for race A, for building the race B session, and for the live message
of race B's paced take; the instant reopen sends no request, and the bench checks that.

**Race A, a long tool chain.** `tau -p` is given one prompt. The fake model asks for 100
`read` calls, one after another (each on a different one of 300 small files), then answers. Both
versions have `read` with a compatible `path` argument. The clock runs from starting the process
to its exit, so it includes starting τ.

**Race B, reopening a large session.** Each version first runs the same scripted conversation, 200
turns of two `read` calls and an answer each, through `tau -p -c`, building a session of 600 model
samples. The race is the TUI reopening it in a terminal pane (tmux, 160x48 for the benchmark), timed until the last
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
  alternating which version goes first. `results.md` records the CPU, kernel, both versions, their Python, Textual and Rich, and the git commit the 0.16 wheel was built from.
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
| `resources.py` | the resource benchmark: CPU, instructions and memory of three clients, writes `resources.md` |
| `visual.py` | the tmux split-pane race a tape runs |
| `tapes/` | one vhs tape per race, and the shared settings |
| `render.sh`, `setup.sh`, `install-tools.sh` | the pipeline |

Only one of `race.py`, `visual.py` and `render.sh` may run at a time: they share the two sandboxes.

## Resources

`resources.py` is separate from the races and the recordings. It asks one question: while a
client shows the same streamed model output, what does it cost? The claim under test is that the
0.16 frontends may use fewer resources than 0.15's, even when the model streams as fast as it can.
`demo/resources.py [--k 5] [--workloads stream,tools,open] [--takes instant,paced] [--force]`
writes `resources.md` and `resources.json` (both git-ignored). Prerequisites are the races' plus
`cargo build --release --locked --manifest-path examples/ratatui/Cargo.toml` and, for instruction
counts, `perf` (below).

**Clients and process topology.** Measured, not assumed: each run's whole client-side process
tree is followed (`/proc/PID/task/*/children`), and the tables break it down by role.

| client | processes |
|---|---|
| 0.15 TUI (`venv15`) | one Python process does everything: UI, driver, provider, tools |
| 0.16 `tau` (Textual) | one Python process: `tau` runs the session in-process, and the Rust core is a library inside it |
| 0.16 ratatui | two processes: `trusty` (Rust, the UI) and a Python `tau serve -d` daemon (session, model calls), joined by a Unix socket. Both count; the per-process table shows the split, so the server's cost is not hidden |

Outside the tree and never counted: the fake model server, tmux, this script. tmux's own CPU is
reported beside each table, as a proxy for what a real terminal emulator would have to do.

**Workloads**, the same scripts for every client (fresh session each run, compaction off, same
model, theme `mocha`, 160x48 pane in a private tmux server, driven by `send-keys`):

* *Stream*: one message, answered with ~4000 tokens of markdown (headings, lists, code blocks, a
  table), in `instant` mode (the fake server sends it as fast as it can) and `paced` (400 ms to
  first token, 70 tokens/s).
* *Tool turn*: 20 sequential `read` calls, then a ~1000-token answer; both takes.
* *Open*: the race B session (200 turns) opened; CPU is from process start to the last turn drawn,
  memory is the settled PSS two seconds after.
* *Idle*: the same run, 30 s with no input, CPU from timers and wakeups.

The window of the stream and tool workloads runs from the Enter that sends the message until the
final marker line is on the tmux screen (the same completion test as the races), after the client
has started and the screen has been still for a second. Startup is therefore **excluded** from
them and **included** in *open*.

**Metrics.** CPU seconds are utime+stime from `/proc/PID/stat`, summed over every process in the tree
(a process that exits keeps the CPU it had at its last sample). PSS and RSS come from
`/proc/PID/smaps_rollup`, sampled every 50 ms; peak is the largest sample of the tree's sum, final
the last. Prefer PSS: it splits memory shared between processes (and the shared Python and C
libraries) in proportion, so a tree is not double counted; RSS counts shared pages in full in each
process and is given for comparison. *Bytes to terminal* are counted by `tmux pipe-pane`: what the
client wrote to its terminal, a proxy for redraw cost that includes the escape sequences. A
real terminal emulator's own CPU and GPU are not measured. Wall time is Enter to marker.

**Instructions.** Retired user-space instructions (`perf stat -e instructions:u -p PID` on each root,
following the threads and children it spawns) are a measure that does not depend on the clock speed or
the load of the machine, beside CPU seconds. They need `perf` and a kernel that lets this user count:
`perf_event_paranoid` must be 1 or lower (`sudo sysctl kernel.perf_event_paranoid=1`, persisted in
`/etc/sysctl.d/`) and, in a VM, a virtual PMU. When that is not so, `resources.md` says so and gives this
fix and the instruction columns read *unavailable*; nothing stands in for them (`--require-perf`
fails the run instead). Instructions are not taken for *open*, where the work happens at process
start, before perf can attach.

**Headline columns.** Each table gives the ratio of CPU seconds and of instructions to 0.15's, the
average share of one core over the window, and CPU ms per 1000 displayed tokens (the answer's
characters over four, as the fake server counts them). The machine's CPU model is in the output:
the figures belong to it.

**Method.** One discarded warm-up run per client, then K runs per cell (default 5), clients
rotating who goes first; median with min and max. The load average is read before and after:
above 2 the run refuses unless `--force`, and the output is then marked DRAFT (so it is for K<3).
The fake server's request digests (as in the races) are compared: every client, in every run of a
workload, must have been sent the same dialogue and tool calls, every request must have streamed,
and the number of requests must be the script's. A client that fails, exits or never shows the
marker fails the run and the pane is printed.

**Caveats.**

* The three clients do not draw the same thing. ratatui prints plain text and does not render
  markdown or highlight code; it has no themes or session picker. 0.15 and 0.16 render markdown. The
  benchmark compares the cost of showing the same words, not of the same pixels.
* The 50 ms sampling misses CPU of a process that lives less than a tick; none of the clients spawns
  one for these workloads (the per-process table would show a `/child` role). The sampler and the
  `capture-pane` polling are this script's own cost, which also lands on the machine and in tmux's CPU.
* PSS and RSS in a pane are one run's; Python's allocator does not return memory eagerly, so final
  is near peak.
* `instant` is a stress case: no model streams that fast. `paced` is nearer to use.
* In *open*, "% of one core" divides CPU since process start by the wall time of the window, which for ratatui begins after its server started: read that cell as meaningless; use CPU seconds.
* The ratatui server starts before the measured window of the stream and tool workloads, but inside *open*.
* The 0.16 wheel in `$DEMO_WORK` is whatever `setup.sh` last built; `resources.md` is only comparable to
  the commit stamped in `wheels/COMMIT`.
