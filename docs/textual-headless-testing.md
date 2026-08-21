# Looking at τ's TUI Without Attaching It to a Terminal

How to see, measure, and test `tau_coding_agent.app.Parley` — the Textual TUI —
without ever handing it a real terminal, plus the live [Textual](https://textual.textualize.io)
devtools a human uses when they *do* have one.

> **Two loops, two audiences.** They are the organizing idea of this document.
>
> | | Loop | Tool |
> |---|---|---|
> | **Agent** | render a named scene → *look at the PNG* → edit `parley.tcss` → re-render | `python -m tau_coding_agent.devshot` (§3) |
> | **Human** | run the app live → save `parley.tcss` → the running app restyles itself | `textual run --dev` (§6) |
>
> The screenshot loop is the agent's; live CSS editing is the human's. Neither
> replaces the other: the agent's loop is reproducible and assertable, the human's
> is interactive and instant.

Verified in this repo against **Textual 8.2.7 / pytest 8.4.2 / cairosvg / textual-dev**,
in the repo venv. Version-sensitive claims are marked; §10 shows how to re-check
them. §6 is explicitly **unverified** — see the banner there.

---

## 1. Why you never attach the TUI to a terminal you also control

Started normally (`tau`, or `App.run()`), Textual seizes the terminal: alternate
screen buffer (`ESC [ ? 1049 h`), mouse tracking, bracketed paste, focus
reporting, synchronized output, hidden cursor, raw/cbreak mode, continuous
repaints, and raw stdin reads. An agent that spawns that in a PTY and tries to
"type" gets a flood of control sequences instead of parseable text, and the agent
and the app fight over the same tty. Piping to a non-tty makes it error or render
garbage.

So: **never screen-scrape a live τ.** Every path in this document goes through
`App.run_test()`, whose `HeadlessDriver` performs *no* terminal I/O.

```python
async with app.run_test(size=(120, 40)) as pilot:
    await pilot.pause()
    assert app.is_headless                                  # True
    assert type(app._driver).__name__ == "HeadlessDriver"   # verified
```

That is safe inside an agent shell, a subprocess, or CI with no PTY at all.

### 1a. Structural prerequisite (already satisfied here)

The `App` subclass must be importable without launching the UI. In τ,
`tau_coding_agent.app.Parley` is a plain class and `.run()` happens in
`tau_coding_agent.cli`, so importing `app.py` never takes over the terminal.
Keep it that way.

---

## 2. The repo's visual-inspection package

Everything below lives in `tau-coding-agent/src/tau_coding_agent/testing/` — the
TUI counterpart of `tau_agent_core.testing` (contract suites for stores). Three
modules, each usable on its own, plus one CLI on top.

```
testing/sandbox.py   build a Parley whose every ~/.tau read/write lands in a temp dir
testing/scenes.py    named app states  +  open_scene(): the one place a scene becomes an app
testing/render.py    a running app -> text grid | SVG | PNG | measured layout dump
devshot.py           CLI: scenes x sizes -> files on disk
```

### 2.1 `testing/sandbox.py` — a hermetic `Parley`

`build_parley(tau_home, *, config=None, discover_extensions=False, extension_paths=(), **kwargs)`
constructs a `Parley` that cannot touch the developer's real `~/.tau`, and
`sandbox_tau_home(root)` is the context manager that redirects it. There are
exactly three moves that isolate a `Parley`, and the long explanation of *why
these three and no others* is the docstring of `tau-coding-agent/tests/conftest.py`:

1. `config.CONFIG_PATH` — the only name `bootstrap_config` actually reads.
   (`config.TAU_DIR` is **not**: `CONFIG_PATH` is computed from it at import time,
   so rebinding `TAU_DIR` afterwards cannot move it.)
2. `session_store.TAU_DIR` — where the file store roots its `sessions/` dir.
3. An **injected** `session_catalog`. `Parley.__init__` documents that an injected
   catalog always wins over resolving one, so the config-driven
   `build_session_catalog` branch — and its live network health check — never runs.

Move 3 is the one that matters most: without it, a machine whose config selects
the JMFTS session store had its *tests* writing into the running JMFTS server.
`sandbox_tau_home` also swaps in a fresh `store._session_listeners` list, because
a real backend registers a module-global session-event listener the app never
unsubscribes.

`sandbox.py` exists because the test suite is no longer the only caller — the
screenshot tool needs the same isolation, and a second hand-rolled copy is how the
two would drift apart. `discover_extensions` defaults **off** so a render never
picks up whatever happens to be in the developer's extensions dir.

### 2.2 `testing/scenes.py` — the scene registry

A **scene** puts a sandboxed `Parley` into ONE known, settled state and nothing
more. Rendering it is `render.py`'s job; deciding what to do with the result is
`devshot`'s or a test's.

```python
@dataclass(frozen=True)
class Scene:
    name: str
    description: str
    arrange: Callable[[App, Pilot], Awaitable[None]] | None = None
    seed:    Callable[[Path], None] | None = None      # runs BEFORE the app is built
    config:  dict[str, Any] = field(default_factory=dict)
```

`seed` runs against the sandbox directory *before* construction, so a mount-time
fetch (the sidebar's session catalog) sees it. `arrange` runs *after* the first
frame has settled and may await the pilot.

`SCENES` is a `tuple[Scene, ...]`; `scene_names()` and `get_scene(name)` are the
accessors (`get_scene` raises `KeyError` naming every valid scene).

#### `open_scene()` — the single shared path, and why that is the design

```python
@asynccontextmanager
async def open_scene(scene: Scene, size: tuple[int, int] = (120, 40)) -> AsyncIterator[tuple[App, Pilot]]:
    ...  # yields (app, pilot) with the frame settled
```

It makes a temp `~/.tau`, enters `sandbox_tau_home`, runs `scene.seed`, builds the
app with `build_parley`, enters `app.run_test(size=size)`, pauses, runs
`scene.arrange`, pauses again, and yields. On exit it removes the temp dir.

**This is the one place a scene becomes a running app, and it is shared
deliberately by the `devshot` CLI and the appearance tests.** The consequence is
the point: a screenshot and an assertion are always looking at *the same frame* —
same sandbox, same size, same settle sequence. A tool with its own private app
construction would eventually screenshot a state no test covers, or assert on a
state no one has ever looked at.

Use it directly whenever you want to poke at a known state:

```python
from tau_coding_agent.testing.scenes import get_scene, open_scene
from tau_coding_agent.testing.render import dump_layout

async with open_scene(get_scene("tree-modal"), (80, 24)) as (app, pilot):
    print(dump_layout(app))
```

#### Rule: host modals in the real app

**A modal composed inside a throwaway `App` loses `CSS_PATH = "parley.tcss"` and
renders full-screen.** A screenshot taken that way flatly contradicts what a user
sees — which makes it worse than no screenshot, because it looks authoritative.
So the modal scenes push the real screen onto the real `Parley`:

```python
async def _open_tree_modal(app, pilot):
    from tau_coding_agent.app import SessionTreeModal
    app.push_screen(SessionTreeModal(_tree_roots()))
    await pilot.pause()
    await pilot.pause()
```

`dump_layout` follows the same rule from the other end: `root` defaults to
`app.screen`, which *is* the modal once one is pushed, so a modal dumps the modal.

(The double `pause()` after `push_screen` is not superstition: the screen mounts
on one loop turn and lays out on the next. §7.2.)

#### Rule: no live data

No clocks, no random ids, no absolute paths, no hostnames. Everything a scene
shows is written into `scenes.py` as frozen fixture text, so two runs produce the
same pixels. This is what makes the scenes usable as snapshot baselines (§5).

#### The scenes as of this writing

`python -m tau_coding_agent.devshot --list`, run in this repo:

```
empty            Fresh app, no saved sessions.
sidebar          Sidebar populated with named sessions, grouped by recency.
answer           One user question and a short text-only answer.
tools            A full exchange (reasoning, two tool calls, results, long answer), collapsed.
tools-expanded   The same exchange with every collapsible open.
tree-modal       The /tree browser over a branching tree.
tree-mode-modal  The mode chooser shown after picking a node.
prompt-editor    The system-prompt editor modal.
ext-surfaces     An extension panel plus two status-bar slots, over a loaded chat.
```

Adding a scene is a `Scene(...)` entry in `SCENES` plus an `arrange` coroutine;
every parametrized appearance test (§4) picks it up automatically.

### 2.3 `testing/render.py` — four views of one frame

All four take an app **already running** inside `run_test()`; none of them start
or stop anything. They all read the same composited frame that
`App.export_screenshot()` renders — `app.screen._compositor.render_update(full=True,
screen_stack=app._background_screens)` — so a pushed modal is included.

| Function | Returns | Use it for |
|---|---|---|
| `render_text(app)` | `str`, one line per terminal row | grepping and diffing. **No image dependency at all.** |
| `render_svg(app, *, title=None)` | `str` (SVG) | `App.export_screenshot()`, colors included |
| `render_png(svg, path, *, scale=2.0, font=DEFAULT_PNG_FONT)` | `Path` | rasterize so a human *or an agent* can simply look at it |
| `dump_layout(app, *, root=None, max_depth=None)` | `str` | "this doesn't fill the screen", "the padding is cluttered" |

Plus two conveniences:

- `save_render(app, out_dir, name, *, title=None, png=True, layout=True)` writes
  every view under `out_dir/<name>.*` and returns the paths it wrote, keyed by
  extension. This is what `devshot` calls.
- `chrome_cost(widgets)` prints, per widget, the vertical rows spent on
  margin + border + padding versus the rows left for content. It is the direct
  measurement behind "the padding is cluttering the screen".

`render_text` **keeps trailing spaces** on purpose: column position is meaningful
when you are looking for a widget that stops short of the screen edge.

`dump_layout` output is one stanza per widget:

```
# 80x24  screen=SessionTreeModal
SessionTreeModal
  xywh=0,0,80,24  margin=.  padding=.  border=.
  Container#tree-browser-dialog
    xywh=0,0,80,24  margin=.  padding=0,1,0,1  border=thick
    Static#tree-browser-title
      xywh=2,1,76,1  margin=0,0,1,0  padding=.  border=.
    Tree#tree-browser-tree
      xywh=2,3,76,18  margin=.  padding=.  border=solid
```

`.` means all-zero spacing or no border. Spacing is `t,r,b,l`. A scrollable widget
whose content overflows also gets `scroll=<virtual>/<region>`. Flags to look for:
`display:none`, `hidden`, and **`not-laid-out`** — a zero region, meaning the
widget is mounted but the compositor gave it no space, which is usually the bug
you are chasing.

---

## 3. `python -m tau_coding_agent.devshot` — the agent's loop

The iteration loop it exists for:

1. `python -m tau_coding_agent.devshot --scene tools --size 120x40`
2. Look at `shots/tools@120x40.png`.
3. Edit `parley.tcss`.
4. Repeat step 1.

Nothing it does touches a real terminal, a real `~/.tau`, or the network.

```bash
python -m tau_coding_agent.devshot --list                         # names + descriptions, then exit
python -m tau_coding_agent.devshot --size 120x40 --size 80x24 --out shots
python -m tau_coding_agent.devshot --scene tools --size 80x24 --out shots
```

| Flag | Meaning |
|---|---|
| `--scene NAME` | repeatable; default **all** scenes |
| `--size WxH` | repeatable; default **`120x40`** |
| `--out DIR` | default `./shots` |
| `--no-png` | skip rasterization (no cairosvg needed) |
| `--list` | print `name  description` for every scene and exit |

Scenes × sizes is a full cross product. Real output of the two-size run above:

```
empty@120x40  ->  shots/empty@120x40.png
empty@80x24  ->  shots/empty@80x24.png
sidebar@120x40  ->  shots/sidebar@120x40.png
...
ext-surfaces@80x24  ->  shots/ext-surfaces@80x24.png
```

An unknown name fails loudly rather than being skipped:

```
python -m tau_coding_agent.devshot: error: unknown scene 'nope'; known scenes: empty,
sidebar, answer, tools, tools-expanded, tree-modal, tree-mode-modal, prompt-editor,
ext-surfaces
```

### 3.1 The four files per render

Each `scene@WxH` writes **four** files:

| File | What it is | What you do with it |
|---|---|---|
| `.png` | the rasterized frame | **look at it** — this is the point |
| `.svg` | `App.export_screenshot()` | the snapshot-plugin format; feeds the PNG |
| `.txt` | the character grid, trailing spaces intact | **grep it**, diff two runs of it |
| `.layout.txt` | every widget's region / margin / padding / border / scroll | measure a layout complaint |

`shots/` is gitignored. It is scratch output, regenerated on demand — the
committed visual baselines are the snapshot SVGs under
`tau-coding-agent/tests/__snapshots__/` (§5), not these.

### 3.2 Animations are pinned off at import

`devshot.py` sets `TEXTUAL_ANIMATIONS=none` **before anything imports textual**,
because Textual reads that variable once, at import time. A moving frame is a
frame that screenshots differently on every run. Note the deliberate
`os.environ.setdefault(...)` above the `# noqa: E402` imports; do not reorder them.

### 3.3 The cairosvg font substitution (why your borders aren't tofu)

Rich writes `font-family: Fira Code, monospace` into every exported SVG. cairosvg
resolves that family name through **cairo's "toy" font API, which does not parse a
comma-separated list** — so it matches neither `Fira Code` nor `monospace`, falls
back to a proportional default with no box-drawing glyphs, and every border in the
screenshot rasterizes as tofu.

`render_png` fixes this with one substitution before rasterizing:

```python
_RICH_SVG_FONT = "font-family: Fira Code, monospace"
DEFAULT_PNG_FONT = "DejaVu Sans Mono"
...
svg = svg.replace(_RICH_SVG_FONT, f"font-family: {font}")
cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(path), scale=scale)
```

Three consequences worth knowing:

- **The substituted family must be installed locally.** If `DejaVu Sans Mono` is
  missing, the PNG comes out with tofu boxes instead of borders — visible in the
  image itself, so it needs no separate check. Override with the `font=` argument
  if your box has something better.
- **Glyph coverage is the substituted font's, not your terminal's.** Verified in
  this repo: the sidebar-toggle glyph `U+2B58 HEAVY CIRCLE` is absent from DejaVu
  Sans Mono and renders as a tofu box in the PNG while being a perfectly fine
  character in the text grid and in a terminal with a fallback font. When a single
  glyph looks wrong in a PNG, check `.txt` before believing the picture.
- **A missing cairosvg raises**, it does not silently skip the PNG:
  `RuntimeError: render_png needs cairosvg; install the dev extra: pip install -e './tau-coding-agent[dev]'`.
  A screenshot tool that silently produces no screenshot is worse than one that stops.

Also note the SVG (and therefore the PNG) is drawn inside a **fake window
decoration** — a titlebar with traffic lights carrying the `title=` string
(`devshot` passes `tau — <scene>`). That chrome is Rich's, not τ's. Do not read
layout measurements off the picture; read them off `.layout.txt`.

---

## 4. Appearance tests: assert on the composited screen

`tau-coding-agent/tests/test_tui_appearance.py` is the assertion half of the same
tooling. A widget query tells you a widget *exists*; it does not tell you the
widget fits on screen, that its text is readable, or that the word on it is the
word you meant. These tests read the same character grid a user looks at.

```python
from tau_coding_agent.testing.render import render_text
from tau_coding_agent.testing.scenes import SCENES, get_scene, open_scene

SIZES = [(120, 40), (80, 24)]

@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("scene", SCENES, ids=lambda s: s.name)
async def test_no_scene_overflows_the_screen(scene, size) -> None:
    async with open_scene(scene, size) as (app, _pilot):
        for index, row in enumerate(render_text(app).splitlines()):
            assert len(row) <= size[0], f"row {index} is {len(row)} cols on a {size[0]}-col screen"
```

Two useful patterns from that file:

- **Whole-registry invariants.** Parametrizing over `SCENES` × `SIZES` means a new
  scene is automatically held to every rule (no row wider than the terminal; the
  rendered screen never says "parley", the fork's name, even though the class may).
- **Measure, don't eyeball.** Density and geometry rules assert on
  `widget.region`, `widget.styles.padding`, `content_size` — e.g. "a collapsed
  collapsible is exactly one row", "the chat text loses at most N columns of chrome
  between the display and the body". These read like the layout dump because they
  are reading the same numbers.

Write appearance rules as *rules*, not as measurements of today's stylesheet. A
test that pins the current pixel count fails on every intentional restyle and
teaches the next person to delete it.

---

## 5. Snapshot tests (`pytest-textual-snapshot`)

`pytest-textual-snapshot` (syrupy backend) renders the app to an **SVG** and diffs
it against a stored reference; installing it auto-registers the **`snap_compare`**
fixture. It is declared in `tau-coding-agent/pyproject.toml`'s `dev` extra.

```python
snap_compare(
    app: str | PurePath | App,                # path to an app .py, OR a live App instance
    press: Iterable[str] = (),
    terminal_size: tuple[int, int] = (80, 24),
    run_before: Callable[[Pilot], Awaitable | None] | None = None,
) -> bool                                     # assert the result
```

Passing an **`App` instance** is the right call here, since a `Parley` worth
snapshotting is a sandboxed one — build it through `testing.sandbox`, not by
pointing the fixture at `app.py`.

`run_before` **must `pause()` before it touches widgets**; without the leading
`await pilot.pause()` a `click()` raises
`textual.pilot.OutOfBounds: Target offset is outside of currently-visible screen region`
because the geometry is not laid out yet (same root cause as §7.2).

### 5.1 Workflow

- **Record / update references:** `pytest --snapshot-update` (the flag comes from
  syrupy). References land in `tests/__snapshots__/<test_module>/<test_name>.svg`
  — for this repo, `tau-coding-agent/tests/__snapshots__/`. **Commit them.**
- **Compare:** a plain `pytest` run. Matching passes; differing **fails**.
- On mismatch the run prints a path to `snapshot_report.html`, a side-by-side of
  expected vs actual. Override with `--snapshot-report <path>`.
- syrupy reports and deletes **orphaned** snapshots on `--snapshot-update`, so
  renamed or removed tests don't leave stale SVGs behind.
- Re-record **deliberately**. A red snapshot after a stylesheet change is the tool
  working. Look at the change (§5.2) before you bless it.

A snapshot suite over the scene registry is being built as of this writing; treat
the directory as the home of committed visual baselines regardless of how many
files are currently in it.

### 5.2 What a coding agent can and cannot see

An earlier version of this document claimed an agent "cannot look at an SVG/HTML
diff". **That is false**, and the whole `devshot` workflow depends on it being
false: `render_png` rasterizes the exported SVG, and an agent reads the resulting
image directly. Looking at the TUI is a first-class, routine operation here.

The residual limitations are narrower and worth stating precisely:

1. **A snapshot *failure* is not a description of the change.** syrupy hands you
   two SVGs and an HTML report; neither says "the modal shrank by four rows". The
   productive move on a red snapshot is to render the same scene with `devshot`
   and compare the `.png` (what changed visually) and the `.layout.txt` (by how
   much) — or `diff` the two `.txt` grids, which is a plain text diff.
2. **The PNG is a rasterization through one substituted font**, so its glyph
   coverage is that font's, not a terminal's (§3.3). A glyph that is tofu in the
   PNG may be fine for a user. Cross-check `.txt`.
3. **The PNG carries Rich's fake window chrome** (§3.3), which is not part of τ.
   Measure geometry from `.layout.txt`, never from the image.

So: use §4 appearance rules and widget-state assertions to prove *behavior*, use
snapshots as the regression guard on *unintended* drift, and use the PNG when the
question is genuinely "does this look right".

### 5.3 Determinism checklist

- **Pin the Textual version.** An upgrade can legitimately change default styling
  and therefore every SVG. Re-record intentionally on upgrade.
- **Fixed `terminal_size`** on every snapshot test.
- **No live data in scenes** — the rule from §2.2 is what makes them snapshotable:
  no clocks, no random ids, no absolute paths, no hostnames, no usernames.
- **Animations off.** `TEXTUAL_ANIMATIONS=none` (`none` | `basic` | `full`;
  default `full`), and it must be set *before* textual is imported (§3.2).
- **Go through `testing.sandbox`.** A `Parley` that reads the developer's real
  `~/.tau` renders that developer's sessions into the baseline.

---

## 6. Live tooling for a human (`textual-dev`)

> ### ⚠ UNVERIFIED — documented, not demonstrated
>
> **Every command in this section needs a real interactive terminal and has not
> been run against τ in this repo.** They were written from the official Textual
> devtools documentation and from `textual --help` / `textual run --help` /
> `textual console --help` / `textual serve --help` in the repo venv — so the
> *flags* below are read off the installed `textual-dev`, but the *behavior* on
> `Parley` specifically is unconfirmed. Treat a surprise here as a bug in this
> section, and fix the section.
>
> Verified only: `textual-dev` is installed in the repo venv and exposes the
> subcommands `borders colors console diagnose easing keys run serve`.

`textual-dev` is declared in `tau-coding-agent/pyproject.toml`'s `dev` extra:

```bash
pip install -e './tau-coding-agent[dev]'
```

### 6.1 `textual run --dev` — live CSS editing (the human's primary loop)

```bash
textual run --dev tau_coding_agent.app:Parley
```

`textual run` accepts a Python import path, and `:Name` selects an app instance or
class other than a module-level `app`. `--dev` enables development mode, whose
headline feature is that **saving `parley.tcss` restyles the running app a few
milliseconds later, with no restart** — you keep your session, your scroll
position, and the modal you had open.

**This is how a human tunes the stylesheet.** The `devshot` loop (§3) is the
agent's equivalent and is reproducible and assertable; live editing is
interactive and instant. Use both for what each is good at: tune live, then lock
the result in as a scene and an appearance rule so it stays tuned.

`--dev` also connects the app to the devtools console (§6.2).

### 6.2 `textual console` — a second terminal for logs

```bash
textual console                       # terminal 1
textual run --dev tau_coding_agent.app:Parley   # terminal 2
```

The console receives the app's `self.log(...)`, its events, and its `print()`
output — all of which are otherwise unreachable, because the app owns the screen
they would have gone to.

| Flag | Meaning |
|---|---|
| `--port PORT` | default 8081 |
| `-v` | verbose logs (normally excluded) |
| `-x, --exclude GROUP` | exclude a log group; repeatable. Groups: `EVENT DEBUG INFO WARNING ERROR PRINT SYSTEM LOGGING WORKER` |

`textual console -x EVENT -x SYSTEM` is the usual starting point — the event
stream is loud. `textual run --dev --host HOST` points the app at a console
running elsewhere.

### 6.3 `textual serve` — the TUI in a browser

```bash
textual serve tau_coding_agent.app:Parley
textual serve --dev -c "tau"        # -c/--command: serve whatever that command launches
```

Serves the app over a local web server; `-h/--host`, `-p/--port`, `-t/--title`,
`-u/--url` are available, and `--dev` enables devtools there too. Useful for
showing the TUI to someone who does not have it installed.

### 6.4 The rest

| Command | What it prints |
|---|---|
| `textual borders` | every border style, rendered |
| `textual colors` | the design system / theme palette |
| `textual keys` | the key events your terminal actually sends (the answer to "why doesn't ctrl+X bind?") |
| `textual diagnose` | Textual, Rich, Python, terminal, and environment versions — **paste this into a bug report** |
| `textual easing` | animation easing functions |

---

## 7. Driving the app: `run_test()`, `Pilot`, and the timing traps

The API underneath `open_scene`. Reach for it directly when a test needs a state
no scene captures.

### 7.1 `App.run_test()` (Textual 8.2.7)

```python
App.run_test(
    *,
    headless: bool = True,                 # leave True — never False in a test or a tool
    size: tuple[int, int] | None = (80, 24),
    tooltips: bool = False,
    notifications: bool = False,
    message_hook: Callable[[Message], None] | None = None,
) -> AsyncContextManager[Pilot]
```

Set `size=` explicitly whenever layout depends on terminal dimensions.
`open_scene` defaults to `(120, 40)`; the appearance suite runs `(120, 40)` and
`(80, 24)`.

### 7.2 `Pilot` and the five flakiness traps

| Method | Signature | Notes |
|---|---|---|
| `press` | `press(*keys: str)` | `"a"`, `"enter"`, `"tab"`, `"escape"`, `"ctrl+c"`, `"shift+tab"`, `"f5"`, … |
| `click` | `click(widget=None, offset=(0,0), shift=False, meta=False, control=False, times=1, button=1)` | `widget` may be a selector string, a `Widget` subclass, or `None` (click at screen `offset`) |
| `double_click` / `triple_click` | as `click` minus `times` | |
| `hover` | `hover(widget=None, offset=(0,0))` | |
| `mouse_down` / `mouse_up` | `(widget=None, offset=(0,0), …)` | drag sequences |
| `pause` | `pause(delay: float | None = None)` | no arg = flush pending messages; with arg = also wait `delay` s |
| `wait_for_animation` / `wait_for_scheduled_animations` | `()` | running / running+scheduled |
| `resize_terminal` | `resize_terminal(width, height)` | simulate a resize mid-test |
| `exit` | `exit(result)` | exit early with `result` |

1. **`pause()` after an action, before asserting.** `press`/`click` post messages;
   the handler may run on the next loop turn.
2. **`pause()` before the first interaction.** Clicking immediately after entering
   `run_test()` can be lost to unfinished first layout — and locating a widget by
   selector can raise `OutOfBounds`. `open_scene` does this for you.
3. **Repeated activation of an animated control needs a real delay.** A `Button`'s
   press-flash lasts ≈0.3 s and re-clicking during it produces no second
   `Button.Pressed`. Measured over 3 clicks: `pause()` → 2, `pause(0.05)` → 2,
   `pause(0.3)` → **3**, `wait_for_animation()` → 1,
   `wait_for_scheduled_animations()` → 2, `TEXTUAL_ANIMATIONS=none` + `pause()` → 2.
   For *logic* tests, drive the state directly instead of simulating N clicks.
   (`TEXTUAL_ANIMATIONS=none` helps snapshot determinism; it does **not** remove
   the press-flash.)
4. **Initial focus swallows global bindings.** The first focusable widget takes
   focus on mount. Verified in every scene: `app.focused` is
   `ChatInput(id='chat-input')`, a `TextArea` — so a plain key press is *typed*
   rather than routed to a binding. Use `app.set_focus(None)`, focus a non-text
   widget, or invoke the action directly. (τ's own global bindings are mostly
   `ctrl+…` and several are `priority=True`, so they survive this; a
   single-character binding would not.)
5. **`return_code` is `None` on teardown, `0` on a real exit.** Leaving the
   `run_test()` block stops the app *without* setting a return code; it only
   becomes `0` if the app actually called `app.exit(...)`. Check it **after** the
   `async with` block.

Background work: `await pilot.app.workers.wait_for_complete()` rather than
guessing a `pause` delay. For bare `asyncio` tasks, poll the observable state in a
short `pause()` loop.

### 7.3 Reading widget state

```python
app.query_one("#status", Label)     # exactly one match of that type, else raises
app.query("ListItem")               # DOMQuery of all matches
app.query_one(Input).value
app.focused                         # or None
app.screen_stack                    # pushed/popped screens
```

> **Textual 8.x:** read a `Static`/`Label`'s text with **`.content`** (`str`).
> `.renderable` is gone (`AttributeError`). Verified in this repo's venv.

---

## 8. Debugging: `pdb` fights the app, unless you're headless

**In a live Textual app, `pdb` fights the terminal for control.** The app owns the
screen, so the debugger's prompt and the app's repaints land on top of each other
and neither is usable. The supported substitute is the devtools console: run the
app with `textual run --dev` and instrument with `self.log(...)` / `self.log.info(...)`,
reading it in `textual console` (§6.2). *(Unverified against τ, like the rest of §6.)*

**Under `run_test()` the driver is headless, and `breakpoint()` works normally.**
Verified in this repo's venv: with `HeadlessDriver` active, `breakpoint()` drops
to a working `(Pdb)` prompt on stdout, DOM queries work at that prompt, and `c`
resumes the app cleanly:

```
driver: HeadlessDriver headless: True
> probe_breakpoint.py(21)main()
-> print("resumed after pdb")
(Pdb) p app.query_one("#l", Label).content
'hello'
(Pdb) p app.size
Size(width=40, height=10)
(Pdb) c
```

That is one more reason to **reproduce a visual bug as a scene before debugging
it**: a scene is a state you can stop inside, step through, query, and screenshot
— and it stays reproducible afterwards, which a live session does not.

Under pytest, `pytest --pdb` and an explicit `breakpoint()` both work in the
appearance/scene tests for the same reason. Add `-s` so pytest does not capture
the debugger's stdout.

---

## 9. Anti-patterns

- ❌ **Spawn τ in a PTY and screen-scrape stdout.** You will parse escape
  sequences and corrupt the terminal. Import `Parley`; use `run_test()` (§1).
- ❌ **`run_test(headless=False)`, or `App.run()` in a test.** Both attach to the
  real terminal.
- ❌ **Compose a modal inside a throwaway `App` for a screenshot.** It loses
  `CSS_PATH` and renders full-screen — an authoritative-looking lie (§2.2).
- ❌ **Hand-roll a second `Parley` sandbox.** Use `testing.sandbox`; a private copy
  is how the tool and the tests drift apart (§2.1). Patching
  `tau_coding_agent.app.TAU_DIR` or `tau_coding_agent.config.TAU_DIR` is a
  **no-op** — see `tests/conftest.py`.
- ❌ **Construct a scene's app inside your own test.** Use `open_scene`, so the
  thing you assert on is the thing that gets screenshotted (§2.2).
- ❌ **Interact before the first `pause()`** (§7.2 trap 2), or **re-click an
  animated control with no delay** (trap 3).
- ❌ **Put a clock, a hostname, or an absolute path in a scene** (§2.2, §5.3).
- ❌ **Read layout measurements off the PNG.** Read `.layout.txt` (§3.3).
- ❌ **`.renderable` on `Static`/`Label`** — it is `.content` in Textual 8.x (§7.3).
- ❌ **`pdb` inside a live `textual run` session** (§8).

---

## 10. Re-verify the version-sensitive claims

```bash
python -c "import textual, pytest; print('textual', textual.__version__, '| pytest', pytest.__version__)"

# run_test + screenshot signatures
python -c "import inspect; from textual.app import App; print(inspect.signature(App.run_test)); print(inspect.signature(App.export_screenshot))"

# Pilot surface
python -c "from textual.pilot import Pilot; print([m for m in dir(Pilot) if not m.startswith('_')])"

# snap_compare's real parameters
python -c "import inspect, pytest_textual_snapshot as p; print(inspect.getsource(p.snap_compare))"

# .content vs .renderable
python -c "from textual.widgets import Label; print('content' in dir(Label), 'renderable' in dir(Label))"

# animation env var + headless driver
python -c "import textual.constants as c; print('TEXTUAL_ANIMATIONS=', c.TEXTUAL_ANIMATIONS, '| levels:', c.AnimationLevel)"

# the dev tooling is actually installed
python -c "import cairosvg, textual_dev; print('cairosvg + textual-dev ok')"
textual --help

# is the substituted PNG font present?
fc-list | grep -c "DejaVu Sans Mono"
```

If a signature here disagrees with what these print, **trust the probes** and fix
this document.

---

## 11. Cheat-sheet

```bash
# --- look at the TUI (agent loop) ---
python -m tau_coding_agent.devshot --list
python -m tau_coding_agent.devshot --scene tools --size 120x40 --size 80x24 --out shots
#   -> shots/tools@120x40.{png,svg,txt,layout.txt}      png: look   txt: grep   layout: measure

# --- assert on it ---
pytest tau-coding-agent/tests/test_tui_appearance.py
pytest tau-coding-agent/tests/ --snapshot-update        # re-record visual baselines, deliberately

# --- tune it live (human loop, UNVERIFIED here) ---
textual console -x EVENT                                 # terminal 1
textual run --dev tau_coding_agent.app:Parley            # terminal 2; save parley.tcss to restyle live
textual diagnose                                         # paste into bug reports
```

```python
# --- a known state, in a test or a REPL ---
from pathlib import Path
from tau_coding_agent.testing.scenes import get_scene, open_scene
from tau_coding_agent.testing.render import render_text, dump_layout, save_render

async with open_scene(get_scene("tree-modal"), (80, 24)) as (app, pilot):
    assert all(len(row) <= 80 for row in render_text(app).splitlines())
    print(dump_layout(app))                       # root defaults to app.screen == the modal
    save_render(app, Path("shots"), "adhoc")      # png + svg + txt + layout
    breakpoint()                                  # works: the driver is headless
```

**Golden rules:** never attach τ to a terminal you control · every scene becomes
an app through `open_scene`, so the screenshot and the assertion see one frame ·
host modals in the real `Parley` · no live data in a scene · look at the `.png`,
grep the `.txt`, measure with `.layout.txt` · agents screenshot, humans
live-edit CSS.
