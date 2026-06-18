# Testing a Python Textual TUI Headlessly (Agent Reference)

A complete, self-contained guide for a coding agent that must test a
[Textual](https://textual.textualize.io) application **without ever running it
interactively**. No web access required.

> Every API signature, behavior, and gotcha below was verified empirically against
> **Textual 8.2.7 / pytest 8.4 / pytest-textual-snapshot 1.1.0 (syrupy backend)**.
> If you are on a different version, see [§10 Verify the API in your environment](#10-verify-the-api-in-your-environment)
> before trusting the specifics — a couple of accessors changed in the 8.x line
> (notably `Static/Label.content`, which replaced the old `.renderable`).

---

## 0. TL;DR — the one rule that matters

**Never launch the TUI attached to a terminal you also control. Import the `App`
subclass and drive it with `App.run_test()`.**

```python
async def test_it():
    async with MyApp().run_test() as pilot:   # headless: no escape sequences, no TTY needed
        await pilot.pause()                    # let the first frame settle
        await pilot.click("#submit")
        await pilot.pause()
        assert pilot.app.query_one("#status").content == "saved"
```

`run_test()` swaps in Textual's **`HeadlessDriver`**, which performs *no* terminal
I/O. Confirmed: inside `run_test()`, `app.is_headless is True` and
`type(app._driver).__name__ == "HeadlessDriver"`. Nothing is written to the real
terminal, so it is safe to run inside an agent shell, a subprocess, or CI with no
PTY at all.

---

## 1. Why naive runs wreck an agent

When you start a Textual app the normal way (`python app.py`, or `App.run()` with
the default `headless=False`), the framework seizes the terminal:

1. Switches to the **alternate screen buffer** (`ESC [ ? 1049 h`).
2. Enables **mouse tracking**, **bracketed paste**, **focus reporting**, and
   **synchronized output** (more escape sequences on every frame).
3. **Hides the cursor** and puts the tty into **raw / cbreak** mode.
4. Repaints continuously with cursor-positioning + SGR color sequences.
5. Reads **raw bytes from stdin**, expecting a real interactive terminal.

If an agent spawns this in a PTY and tries to "type" on stdin and read stdout, it
gets a flood of control sequences instead of parseable text, the terminal's state
gets corrupted, and the agent and the app **fight over the same terminal**.
Piping to a non-tty often makes the app error or render garbage instead.

**The fix is never to screen-scrape the live app.** Use the headless test driver
(`run_test()`), the snapshot plugin, or `export_screenshot()`. All three are
described below.

### 1a. Structural prerequisite (do this first)

Make the `App` subclass importable **without** launching the UI. Keep the
`run()` call behind a `__main__` guard so importing the module for tests does not
take over the terminal:

```python
# app_demo.py
class MyApp(App):
    ...

if __name__ == "__main__":   # <-- only runs on `python app_demo.py`, never on import
    MyApp().run()
```

If the entrypoint currently does work at import time, refactor so the `App`
subclass and its construction are separable from `.run()`.

---

## 2. Environment setup

```bash
pip install textual pytest pytest-asyncio pytest-textual-snapshot
```

Tests are `async`, so pytest needs async support. Configure auto mode so any
`async def test_*` is collected automatically (no per-test marker needed):

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"   # silences a pytest-asyncio deprecation warning
```

(Equivalent `pytest.ini`: `[pytest]` with `asyncio_mode = auto`.)

Recommended layout:

```
project/
├── app_demo.py
├── pyproject.toml
└── tests/
    ├── test_interactions.py
    ├── test_snapshots.py
    └── __snapshots__/            # auto-created reference SVGs (commit these)
        └── test_snapshots/
            ├── test_initial_render.svg
            └── test_after_click.svg
```

---

## 3. Approach 1 — Programmatic interaction tests with `Pilot` (primary method)

This is the agent-friendly path: drive the app with a `Pilot`, then **assert on
widget state via the query API** rather than parsing any rendered output.

### 3.1 `App.run_test()` signature (verified)

```python
App.run_test(
    *,
    headless: bool = True,                 # leave True — never set False for agents
    size: tuple[int, int] | None = (80, 24),
    tooltips: bool = False,
    notifications: bool = False,
    message_hook: Callable[[Message], None] | None = None,
) -> AsyncContextManager[Pilot]
```

Set `size=` explicitly whenever layout depends on terminal dimensions — it makes
tests deterministic regardless of the host terminal.

### 3.2 The `Pilot` API (verified signatures)

Reach the running app with **`pilot.app`**, then use the normal query/state API.

| Method | Signature | Notes |
|---|---|---|
| `press` | `press(*keys: str) -> None` | `"a"`, `"enter"`, `"tab"`, `"escape"`, `"backspace"`, `"up"`, `"space"`, `"ctrl+c"`, `"shift+tab"`, `"f5"`, … |
| `click` | `click(widget=None, offset=(0,0), shift=False, meta=False, control=False, times=1, button=1) -> bool` | `widget` may be a CSS selector string, a `Widget` subclass, or `None` (click at screen `offset`). `times=2` double-clicks. |
| `double_click` / `triple_click` | same args as `click` minus `times` | convenience wrappers |
| `hover` | `hover(widget=None, offset=(0,0)) -> bool` | |
| `mouse_down` / `mouse_up` | `(widget=None, offset=(0,0), shift=False, meta=False, control=False, button=1) -> bool` | for drag-style sequences |
| `pause` | `pause(delay: float | None = None) -> None` | **no arg** = flush currently-pending messages; **with arg** = also wait `delay` seconds (lets scheduled timers/animations run) |
| `wait_for_animation` | `wait_for_animation() -> None` | waits for *running* animations |
| `wait_for_scheduled_animations` | `wait_for_scheduled_animations() -> None` | waits for running **and** scheduled animations |
| `resize_terminal` | `resize_terminal(width: int, height: int) -> None` | simulate a resize mid-test |
| `exit` | `exit(result) -> None` | exit the app early, returning `result` |

`pilot.app` exposes everything you need for assertions: `query_one`, `query`,
`focused`, `screen`, `screen_stack`, `title`, `return_code`, `workers`, etc.

### 3.3 Querying & reading widget state

Same CSS-like selectors as in the app itself:

```python
pilot.app.query_one("#status", Label)      # exactly one match of the given type, else raises
pilot.app.query("ListItem")                # DOMQuery (list-like) of all matches
pilot.app.query_one(Input).value           # Input text
pilot.app.focused                          # currently-focused widget (or None)
pilot.app.screen_stack                     # for testing pushed/popped screens
```

> **Textual 8.x content accessor:** read a `Static`/`Label`'s text with
> **`.content`** (returns `str`). The old **`.renderable`** attribute is gone
> (`AttributeError: 'Label' object has no attribute 'renderable'`). On 7.x and
> earlier, use `.renderable`. Verify with §10 if unsure.

### 3.4 A complete, runnable example

```python
# app_demo.py
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Input, Label

class CounterApp(App[int]):
    CSS = "#count { padding: 1; }"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Label("0", id="count")
        yield Input(placeholder="name", id="name")
        with Horizontal():
            yield Button("Increment", id="inc", variant="success")
            yield Button("Reset", id="reset", variant="error")

    def on_mount(self) -> None:
        self.count = 0

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "inc":
            self.count += 1
        elif event.button.id == "reset":
            self.count = 0
        self.query_one("#count", Label).update(str(self.count))

if __name__ == "__main__":
    CounterApp().run()
```

```python
# tests/test_interactions.py
from app_demo import CounterApp
from textual.widgets import Input, Label

async def test_increment_button_updates_label():
    async with CounterApp().run_test() as pilot:
        await pilot.pause()            # settle the first frame before interacting
        await pilot.click("#inc")
        await pilot.pause(0.3)         # wait out Button's press-flash before re-click (see §4)
        await pilot.click("#inc")
        await pilot.pause()
        assert pilot.app.query_one("#count", Label).content == "2"   # .content in Textual 8.x

async def test_reset_button():
    async with CounterApp().run_test() as pilot:
        await pilot.pause()
        await pilot.click("#inc")
        await pilot.pause(0.3)
        await pilot.click("#reset")
        await pilot.pause()
        assert pilot.app.query_one("#count", Label).content == "0"

async def test_typing_into_input():
    async with CounterApp().run_test() as pilot:
        pilot.app.set_focus(pilot.app.query_one("#name", Input))
        await pilot.press("a", "d", "a")
        assert pilot.app.query_one("#name", Input).value == "ada"

async def test_quit_binding_when_focus_cleared():
    app = CounterApp()
    async with app.run_test() as pilot:
        pilot.app.set_focus(None)     # clear focus so the key bubbles to bindings (see §4)
        await pilot.press("q")
        await pilot.pause()
    assert app.return_code == 0       # clean exit via the quit action
```

All of the above pass on Textual 8.2.7.

---

## 4. The timing model & the gotchas that make tests flaky

Simulated input is processed by the **same event loop and the same animation
timing** as real input. These five behaviors are the usual cause of
"works-once, fails-twice" flakiness. Each was reproduced and confirmed.

### 4.1 Always `pause()` after an action before asserting
`press`/`click` post messages; the handler may run on the *next* loop turn.
`await pilot.pause()` (no arg) flushes pending messages. Assert **after** it.

### 4.2 The first interaction can be dropped — open with a `pause()`
Clicking/pressing immediately after entering `run_test()` can be lost because the
app has not finished its first layout/refresh. **Begin every test with
`await pilot.pause()`** (or `run_test(...)` then `pause`) before the first
interaction. Symptom without it: the first of N actions silently doesn't register.

### 4.3 Repeated activation of an animated widget needs a real delay
A `Button` shows a **press-flash** (active state) lasting ≈0.3 s. Re-clicking the
*same* button while it is still flashing does **not** produce a fresh
`Button.Pressed`. Measured, 3 clicks on one button:

| inter-click wait | resulting count |
|---|---|
| `pause()` | 2 ❌ |
| `pause(0.05)` | 2 ❌ |
| `pause(0.3)` | **3 ✅** |
| `wait_for_animation()` | 1 ❌ |
| `wait_for_scheduled_animations()` | 2 ❌ |
| `TEXTUAL_ANIMATIONS=none` + `pause()` | 2 ❌ |

So for **repeated** activation of the same animated control, either insert
`await pilot.pause(0.3)` between activations, **or** (preferred for deterministic
*logic* tests) drive the state directly instead of simulating N clicks — e.g.
assert "one click ⇒ +1" once, or post the message / call the action handler
yourself. Reserve full input simulation for genuine input-path tests.
(Note: `TEXTUAL_ANIMATIONS=none` helps snapshot *motion* determinism but does
**not** remove the press-flash timing.)

### 4.4 Initial focus can swallow your key bindings
The first focusable widget gets focus on mount. In the example app that is the
`Input`, so `await pilot.press("q")` is **typed into the Input** and the `q→quit`
binding never fires (`app.return_code` stays `None`). Confirmed both ways:

```python
# 'q' swallowed by the focused Input -> binding never fires
async with CounterApp().run_test() as pilot:
    await pilot.press("q"); await pilot.pause()
assert app.return_code is None

# clear focus first -> key bubbles to the app binding -> clean exit
async with CounterApp().run_test() as pilot:
    pilot.app.set_focus(None)
    await pilot.press("q"); await pilot.pause()
assert app.return_code == 0
```

When testing a global binding, make sure focus is somewhere that won't capture the
key (`set_focus(None)`, or focus a non-text widget), or invoke the action directly.

### 4.5 `return_code` is `None` on a forced teardown, `0` on a real exit
Leaving the `run_test()` block stops the app **without** setting a return code, so
`app.return_code is None`. It only becomes `0` (or your value) if the app actually
called `app.exit(...)` (e.g. via a quit action) before the block ended. Check
`return_code` **after** the `async with` block.

### 4.6 Background workers — wait deterministically
If the app uses `@work` workers/timers, block until they finish rather than
guessing a `pause` delay:

```python
async with WorkerApp().run_test() as pilot:
    await pilot.app.workers.wait_for_complete()   # WorkerManager.wait_for_complete(workers=None)
    await pilot.pause()
    assert pilot.app.query_one("#status", Label).content == "done"
```

Confirmed working. If your code uses bare `asyncio` tasks or timers instead of
workers, poll the observable state in a short `pause()` loop.

---

## 5. Approach 2 — SVG snapshot / visual-regression tests

`pytest-textual-snapshot` (built on `syrupy`) renders the app to an **SVG** and
diffs it against a stored reference. Installing the package auto-registers the
**`snap_compare`** fixture.

### 5.1 `snap_compare` signature (verified)

```python
snap_compare(
    app: str | PurePath | App,                 # path to the app .py, OR a live App instance
    press: Iterable[str] = (),                  # keys to send before the screenshot
    terminal_size: tuple[int, int] = (80, 24),
    run_before: Callable[[Pilot], Awaitable | None] | None = None,  # arbitrary pilot driving
) -> bool                                       # assert the result
```

```python
# tests/test_snapshots.py
from pathlib import Path
APP = Path(__file__).parent.parent / "app_demo.py"

def test_initial_render(snap_compare):
    assert snap_compare(APP, terminal_size=(60, 12))

def test_with_keys(snap_compare):
    assert snap_compare(APP, terminal_size=(60, 12), press=["tab", "enter"])

async def _after_increment(pilot):
    await pilot.pause()          # REQUIRED: settle layout before locating widgets...
    await pilot.click("#inc")    # ...or you get textual.pilot.OutOfBounds
    await pilot.pause()

def test_after_click(snap_compare):
    assert snap_compare(APP, terminal_size=(60, 12), run_before=_after_increment)
```

> **`run_before` must `pause()` before it touches widgets.** Without the leading
> `await pilot.pause()`, `click("#inc")` raised
> `textual.pilot.OutOfBounds: Target offset is outside of currently-visible
> screen region` because the widget's geometry wasn't laid out yet. Adding the
> pause fixed it. (Same root cause as §4.2.)

> **Passing an `App` instance** instead of a file path is cleaner when the app
> needs constructor args: `snap_compare(MyApp(config=...), ...)`. A path string
> causes the file to be re-executed to locate the `App` subclass.

### 5.2 Workflow & where things live (verified)

- **Create / update references:** `pytest --snapshot-update` (the `--snapshot-update`
  flag comes from syrupy). References are written to
  **`tests/__snapshots__/<test_module>/<test_name>.svg`**. **Commit them.**
- **Compare (normal run):** `pytest`. Matching ⇒ pass; differing ⇒ **fail**.
- On mismatch the run prints a path to **`snapshot_report.html`** (an HTML
  side-by-side of expected vs actual). Override the location with
  `--snapshot-report <path>`.
- syrupy **also reports and deletes orphaned** snapshots (`N unused snapshot
  deleted`) on `--snapshot-update`, so renamed/removed tests don't leave stale
  SVGs.

Confirmed end-to-end: after recording, changing a button's label
(`"Increment"` → `"Add one"`) flipped the affected snapshot tests to **failed**
and emitted the report path; re-recording with `--snapshot-update` restored green.

### 5.3 Snapshots and a coding agent — important caveat

An agent **cannot "look at" an SVG/HTML diff** the way a human can. Treat
snapshots as a **regression guard**, not a correctness oracle:

- Use **§3 Pilot + state assertions** to prove the app *behaves* correctly.
- Use snapshots to catch *unintended visual drift* once a known-good state is
  captured. When one fails, the agent can diff the **SVG text** of
  expected-vs-actual (it's XML — see §6) to describe *what* changed, even without
  rendering the image.

### 5.4 Determinism checklist (or snapshots will be flaky)

- **Pin the Textual version.** A Textual upgrade can legitimately change default
  styling/theme and therefore every SVG. Re-record intentionally on upgrade.
- **Fixed `terminal_size`** on every snapshot test.
- **Seed all randomness** (`random.seed(...)`, fixed UUIDs).
- **Freeze time / avoid clocks & timestamps** in the UI (e.g. `freezegun`, or
  inject a fixed clock). Live timestamps guarantee diffs.
- **Wait out animations**, or set `TEXTUAL_ANIMATIONS=none` to disable motion
  (values: `none` | `basic` | `full`; default `full`). Take the screenshot only
  after the UI is at rest.
- **Avoid host-specific content** in the UI (absolute paths, usernames, hostnames).

---

## 6. Approach 3 — Manual SVG capture + text assertions

When you want a screenshot without the snapshot machinery, or want to assert on
*visible text* without rendering an image:

```python
App.export_screenshot(*, title: str | None = None, simplify: bool = False) -> str   # returns SVG
App.save_screenshot(filename=None, path=None, time_format=None) -> str              # writes SVG to disk
```

```python
async def test_visible_text_via_svg():
    async with CounterApp().run_test(size=(60, 12)) as pilot:
        await pilot.pause()
        svg = pilot.app.export_screenshot()
        assert "Increment" in svg          # SVG <text> nodes carry the rendered glyphs
```

Confirmed: the exported SVG's `<text>` elements contain the on-screen characters,
so substring checks (or `xml.etree.ElementTree` parsing of `<text>` nodes) let an
agent assert on what is *actually visible* — useful when state isn't directly
queryable. Prefer direct widget queries (§3) when you can; reach for this when you
specifically need the composited/visible result.

---

## 7. Approach 4 — Test a single widget in isolation

Wrap one widget in a throwaway host `App` so you don't need the whole application:

```python
from textual.app import App, ComposeResult
from textual.widgets import Button

class ButtonHarness(App):
    def compose(self) -> ComposeResult:
        yield Button("Click me", id="go")

async def test_button_label():
    async with ButtonHarness().run_test() as pilot:
        assert pilot.app.query_one("#go", Button).label.plain == "Click me"
```

This keeps widget unit tests fast and focused, and is the natural place to test a
custom widget's reactives, messages, and rendering.

---

## 8. Running the tests

```bash
pytest                       # run everything; snapshots compared
pytest -q tests/test_interactions.py
pytest --snapshot-update     # (re)record SVG references
pytest --snapshot-report build/snap.html   # custom report path
```

- **No TTY required.** `run_test()` is headless, so this works in CI, containers,
  and under an agent with no controlling terminal. (Sanity check:
  `app.is_headless is True` inside `run_test()`.)
- Async tests run automatically because of `asyncio_mode = "auto"` (§2). If you
  prefer explicit markers, drop the auto mode and decorate each with
  `@pytest.mark.asyncio`.
- The whole event loop is in-process — `print`/`logging` from tests goes to
  pytest's captured stdout as usual; the app's `self.log(...)` goes to the
  Textual devtools console (not needed for headless assertions).

---

## 9. Anti-patterns (do **not** do these)

- ❌ **Spawn the app in a PTY/subprocess and screen-scrape stdout.** You'll fight
  over control sequences and parse garbage. Import the `App` and use `run_test()`.
- ❌ **`App.run()` / `run_test(headless=False)` in tests.** Both attach to the real
  terminal. Headless is the default for `run_test()`; keep it.
- ❌ **Assert on raw escape-sequence output.** Assert on widget state (§3) or on
  SVG text/snapshots (§5–6).
- ❌ **Interact before the first `pause()`.** The opening action gets dropped (§4.2).
- ❌ **Re-click the same animated control back-to-back with no delay** and expect
  every activation to register (§4.3).
- ❌ **Use `.renderable` on Static/Label in Textual 8.x.** It's `.content` now (§3.3).
- ❌ **Leave timestamps/RNG/animations live in snapshot targets.** Guaranteed flake (§5.4).
- ❌ **Do real work at module import.** Guard `.run()` behind `if __name__ == "__main__":` (§1a).

---

## 10. Verify the API in your environment

You have no web access, so confirm anything version-sensitive directly against the
installed package (these are the exact probes used to validate this document):

```bash
python -c "import textual, pytest; print('textual', textual.__version__, '| pytest', pytest.__version__)"

# run_test + screenshot signatures
python -c "import inspect; from textual.app import App; print(inspect.signature(App.run_test)); print(inspect.signature(App.export_screenshot)); print(inspect.signature(App.save_screenshot))"

# Pilot surface + a specific method
python -c "from textual.pilot import Pilot; print([m for m in dir(Pilot) if not m.startswith('_')])"
python -c "import inspect; from textual.pilot import Pilot; print(inspect.signature(Pilot.click))"

# snap_compare's real parameters (inner 'compare' fn)
python -c "import inspect, pytest_textual_snapshot as p; print(inspect.getsource(p.snap_compare))"

# how this Textual version exposes a widget's text (e.g. .content vs .renderable)
python -c "from textual.widgets import Label; print('content' in dir(Label), 'renderable' in dir(Label))"

# animation env var + headless driver location
python -c "import textual.constants as c; print('TEXTUAL_ANIMATIONS=', c.TEXTUAL_ANIMATIONS, '| levels:', c.AnimationLevel)"
python -c "from textual.drivers.headless_driver import HeadlessDriver; print(HeadlessDriver.__module__)"
```

If a signature here disagrees with what these print, **trust the probes** — they
reflect the version you're actually running.

---

## 11. Cheat-sheet

```python
# --- drive headlessly ---
async with MyApp(...).run_test(size=(100, 40)) as pilot:
    await pilot.pause()                         # settle first frame (do this first)
    await pilot.press("tab", "enter", "ctrl+s") # keys
    await pilot.click("#save")                  # mouse (selector | Widget | None)
    await pilot.pause(0.3)                       # re-activating same animated widget? wait the flash
    await pilot.app.workers.wait_for_complete() # background workers done
    # --- assert on STATE, not scraped output ---
    assert pilot.app.query_one("#status", Label).content == "saved"   # .content (Textual 8.x)
    assert pilot.app.query_one(Input).value == "..."
    assert pilot.app.focused.id == "save"
# after the block:
assert app.return_code == 0   # None unless the app actually exit()-ed

# --- visual regression ---
def test_view(snap_compare):
    assert snap_compare(MyApp(), terminal_size=(100, 40), press=["enter"])
# record/update:  pytest --snapshot-update     compare:  pytest
# references:     tests/__snapshots__/<module>/<test>.svg
# on failure:     snapshot_report.html  (override: --snapshot-report PATH)

# --- visible text without a snapshot ---
svg = pilot.app.export_screenshot(); assert "Expected" in svg
```

**Golden rules:** import the `App` and use `run_test()` (never scrape a live
terminal) · open with `pause()` · `pause()` after every action before asserting ·
delay between repeated activations of animated widgets · assert on widget state,
keep SVG snapshots as a regression guard · pin the Textual version for snapshots.
