#!/usr/bin/env python3
"""The split-pane race, drawn in the terminal it is run from (a vhs tape runs it).

    visual.py a|b instant|paced

A tmux layout, left τ 0.15 and right τ 0.16, each pane headed by `tau --version`. One signal
releases both at once. The pane borders carry what each is doing: calls made so far (race A),
the time elapsed, and the time each finished. Everything runs under the demo work directory in
two sandboxes against two fake servers that replay the same script.

    visual.py pane a|b VERSION RUNDIR     (what each pane runs; not for direct use)
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import common
import scenario
from tmuxdrv import Tmux, choose_session

HOLD = 6.0  # seconds the finished race stays on screen
COUNTDOWN = 3
COLOR = {"15": "#fab387", "16": "#a6e3a1"}  # Catppuccin peach and green
NAME = {"15": "0.15", "16": "0.16"}


# ------------------------------------------------------------ the pane side


def pane(race: str, version: str, rundir: Path) -> None:
    tau = str(common.tau_bin(version))
    env = common.env_for(version)
    os.chdir(common.fixture_dir())
    print("$ tau --version", flush=True)
    subprocess.run([tau, "--version"], env=env, check=True)
    shown = (
        f'tau -p "{scenario.PROMPT_A}"'
        if race == "a"
        else " ".join(["tau", *common.tui_cmd(version)[1:]])
    )
    print(f"\n$ {shown}", flush=True)
    sock = (rundir / "sock").read_text()
    subprocess.run(["tmux", "-L", sock, "wait-for", "go"], check=True)
    t0 = time.perf_counter()
    if race == "b":
        os.execvpe(tau, common.tui_cmd(version), {**env, "COLORTERM": "truecolor"})
    subprocess.run(common.print_cmd(version, [scenario.PROMPT_A]), env=env, check=True)
    elapsed = time.perf_counter() - t0
    (rundir / f"done-{version}").write_text(f"{elapsed:.3f}")
    print(f"\n\x1b[1;32m✓ finished in {elapsed:.2f} s\x1b[0m", flush=True)
    time.sleep(3600)


# ------------------------------------------------------- the controller side


class Race:
    def __init__(self, race: str, take: str) -> None:
        self.race, self.paced = race, take == "paced"
        self.tmux = Tmux()
        self.rundir = common.WORK / f"run-{os.getpid()}"
        self.ports: dict[str, int] = {}
        self.finish: dict[str, dict[str, float]] = {v: {} for v in common.VERSIONS}
        self.t0 = 0.0
        self.errors: list[str] = []
        self.lock = threading.Lock()

    def label(self, v: str, state: str) -> None:
        self.tmux.run("set-option", "-w", "-t", "v", f"@l{v}", f" τ {NAME[v]}   {state} ")

    def banner(self, text: str) -> None:
        self.tmux.run("set-option", "-t", "v", "status-left", f" {text} ")

    def stats_calls(self, v: str) -> int:
        try:
            return int(common.server_stats(self.ports[v])["served"])
        except OSError:
            return 0

    def state(self, v: str, now: float) -> str:
        done = self.finish[v]
        elapsed = f"{(done.get('total') or done.get('answered') or done.get('drawn') or now - self.t0):6.2f} s"
        parts = []
        if self.race == "a":
            n = scenario.CALLS_A + 1
            parts.append(f"{min(self.stats_calls(v), n):>3}/{n} calls")
        if "total" in done:
            return "✓ " + " · ".join([*parts, f"finished {done['total']:.2f} s"])
        if "drawn" in done:
            parts.append(f"drawn {done['drawn']:.2f} s")
        if "answered" in done:
            parts.append(f"answered {done['answered']:.2f} s")
            return "✓ " + " · ".join(parts)
        if "drawn" not in done and self.race == "b":
            parts.append("opening")
        return " · ".join([*parts, elapsed]) if self.race == "a" or not done else " · ".join(parts)

    def watch_b(self, v: str, pane_id: str) -> None:
        try:
            self.watch_b_pane(v, pane_id)
        except Exception as e:  # reaches track() through self.errors: a thread's exception does not
            self.errors.append(f"0.{v}: {e}")

    def watch_b_pane(self, v: str, pane_id: str) -> None:
        done = self.finish[v]
        choose_session(self.tmux, pane_id, v)
        done["drawn"] = self.tmux.wait_for(pane_id, scenario.FINISH_B, 900) - self.t0
        if self.paced:
            time.sleep(1.0)  # a person reaches for the keyboard
            self.tmux.run("send-keys", "-t", pane_id, "-l", scenario.PROMPT_LIVE)
            time.sleep(0.7)
            self.tmux.run("send-keys", "-t", pane_id, "Enter")
            done["answered"] = self.tmux.wait_for(pane_id, scenario.FINISH_LIVE, 900) - self.t0

    def finished(self) -> bool:
        key = "total" if self.race == "a" else ("answered" if self.paced else "drawn")
        return all(key in self.finish[v] for v in common.VERSIONS)

    def winner_text(self) -> str:
        key = "total" if self.race == "a" else "drawn"
        t15, t16 = (self.finish[v][key] for v in common.VERSIONS)
        what = "to finish" if self.race == "a" else "to reopen the session"
        fast, slow = ("0.16", "0.15") if t16 < t15 else ("0.15", "0.16")
        ratio = max(t15, t16) / min(t15, t16)
        return f"Race finished: τ {fast} took {ratio:.1f}x less time than τ {slow} {what}"

    def run(self) -> None:
        script = common.WORK / ("script-a.json" if self.race == "a" else "script-b.json")
        scenario.prepare(common.WORK)
        remove_stale_rundirs()
        self.rundir.mkdir()
        (self.rundir / "sock").write_text(self.tmux.socket)
        servers = {v: common.fake_server(script, self.paced) for v in common.VERSIONS}
        try:
            for v, s in servers.items():
                self.ports[v] = s.__enter__()
                common.write_config(v, self.ports[v], "mocha")
                if self.race == "b":
                    restore_session(v)
            self.layout()
            threading.Thread(target=self.attach_hold, daemon=True).start()
            for n in range(COUNTDOWN, 0, -1):
                for v in common.VERSIONS:
                    self.label(v, f"starts in {n}")
                time.sleep(1)
            self.tmux.run("wait-for", "-S", "go")
            self.t0 = time.perf_counter()
            self.banner(
                "Race A: one prompt, 100 sequential tool calls"
                if self.race == "a"
                else "Race B: reopen a 200-turn session"
            )
            self.track()
        finally:
            for s in servers.values():
                s.__exit__(None, None, None)
            self.tmux.kill()
            shutil.rmtree(self.rundir, ignore_errors=True)

    def layout(self) -> None:
        t = self.tmux
        py = shlex.quote(sys.executable)
        me = shlex.quote(str(Path(__file__).resolve()))
        env = f"env -i PATH={shlex.quote(os.environ['PATH'])} TERM=xterm-256color LANG=C.UTF-8 DEMO_WORK={shlex.quote(str(common.WORK))}"

        def cmd(v: str) -> str:
            return f"exec {env} {py} {me} pane {self.race} {v} {shlex.quote(str(self.rundir))}"

        cols, rows = os.get_terminal_size()
        t.start("-s", "v", "-x", str(cols), "-y", str(rows), cmd("15"))
        t.run("split-window", "-h", "-t", "v", cmd("16"))
        panes = t.run("list-panes", "-t", "v", "-F", "#{pane_id}").split()
        self.panes = dict(zip(common.VERSIONS, panes, strict=True))
        fmt = (
            f"#{{?#{{==:#{{pane_index}},0}},#[fg={COLOR['15']}#,bold]#{{@l15}},"
            f"#[fg={COLOR['16']}#,bold]#{{@l16}}}}"
        )
        for k, val in (
            ("status-style", "bg=#181825,fg=#cdd6f4,bold"),
            ("status-left-length", "200"),
            ("status-right", ""),
            ("status-position", "bottom"),
        ):
            t.run("set-option", "-g", k, val)
        for k, val in (
            ("window-status-format", ""),
            ("window-status-current-format", ""),
            ("pane-border-lines", "heavy"),
            ("pane-border-style", "fg=#585b70"),
            ("pane-active-border-style", "fg=#585b70"),
            ("pane-border-status", "top"),
            ("pane-border-format", fmt),
        ):
            t.run("set-option", "-w", "-g", k, val)
        for v in common.VERSIONS:
            self.label(v, "ready")
        self.banner("τ 0.15  vs  τ 0.16")

    def check_alive(self) -> None:
        """Abort the race, with what the pane left on screen, if a pane's program died or a
        watcher failed: a race that cannot finish must not hang the recording."""
        if self.errors:
            raise SystemExit("race failed: " + "; ".join(self.errors))
        for v, pane_id in self.panes.items():
            gone = self.tmux.dead(pane_id)
            if gone is not None and not (self.race == "a" and "total" in self.finish[v]):
                raise SystemExit(f"race failed: 0.{v}'s pane died: {gone}")

    def attach_hold(self) -> None:
        subprocess.run(["tmux", "-L", self.tmux.socket, "attach", "-t", "v"], check=False)

    def track(self) -> None:
        if self.race == "b":
            for v in common.VERSIONS:
                threading.Thread(target=self.watch_b, args=(v, self.panes[v]), daemon=True).start()
        while not self.finished():
            self.check_alive()
            now = time.perf_counter()
            if self.race == "a":
                for v in common.VERSIONS:
                    f = self.rundir / f"done-{v}"
                    if f.exists() and "total" not in self.finish[v]:
                        self.finish[v]["total"] = float(f.read_text())
            for v in common.VERSIONS:
                self.label(v, self.state(v, now))
            time.sleep(0.1)
        for v in common.VERSIONS:
            self.label(v, self.state(v, time.perf_counter()))
        self.banner(self.winner_text())
        time.sleep(HOLD)


def remove_stale_rundirs() -> None:
    """Run directories of runs that were killed (their pid is gone)."""
    for d in common.WORK.glob("run-*"):
        try:
            os.kill(int(d.name.removeprefix("run-")), 0)
        except ProcessLookupError:
            shutil.rmtree(d, ignore_errors=True)
        except (ValueError, PermissionError):
            continue


def restore_session(version: str) -> None:
    home = common.sandbox(version) / ".tau"
    if not (home / "sessions.pristine").exists():
        raise SystemExit("no large session: run demo/race.py build-b")
    shutil.rmtree(home / "sessions", ignore_errors=True)
    shutil.copytree(home / "sessions.pristine", home / "sessions")


def main() -> None:
    if sys.argv[1] == "pane":
        pane(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    else:
        Race(sys.argv[1], sys.argv[2]).run()


if __name__ == "__main__":
    main()
