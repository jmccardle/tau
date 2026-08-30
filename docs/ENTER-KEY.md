# The Enter key

**Built 2026-08-29.** τ's chat editor uses Enter for a line break and Ctrl+J to
send. pi, Claude Code, and every other terminal coding agent use the opposite
pair. `enter_key` in `~/.tau/config.json` selects one. This document records why
the two conventions exist, what a terminal can and cannot tell an application
about a modifier on Enter, and where in Textual the swap has to live.

Everything measured here was measured against **Textual 8.2.7**, the version τ
pins. Section 6 is a probe you can run to check your own terminal.

---

## 1. There are only two bytes

A terminal keyboard does not send key names. It sends bytes.

| Key | Byte | Name |
|---|---|---|
| Enter | `0x0D` | CR, carriage return, also Ctrl+M |
| Ctrl+J | `0x0A` | LF, line feed |

That is the whole legacy vocabulary for this key. Enter and Ctrl+M are the same
byte. There is no third byte, and in particular there is no byte for Shift+Enter:
a terminal with no extension sends `0x0D` whether Shift is held or not.

This is the origin of the frustration. It is not that applications ignore
Shift+Enter. It is that in the legacy encoding the keystroke does not survive the
trip to the application at all.

Textual names these two bytes:

```
'\r'  ->  key "enter"    (alias "ctrl+m")
'\n'  ->  key "ctrl+j"   (alias "newline")
```

Those aliases are `textual/keys.py:251` and `:254`.

## 2. The kitty keyboard protocol makes the modifier real

The limit above is a property of the legacy encoding, not of terminals in
general. The kitty keyboard protocol replaces the bare byte with a CSI-u
sequence that carries the modifier bits, and Textual asks for it.

`textual/drivers/linux_driver.py` writes `\x1b[>25u` on startup — flags `1 | 8 |
16`, disambiguate + report-all-keys + report-associated-text — and `\x1b[<u` on
exit. A terminal that does not implement the protocol ignores the sequence, and
nothing else changes.

When the terminal does implement it, Textual's parser produces:

| Sequence | Textual key |
|---|---|
| `\x1b[13u` | `enter` |
| `\x1b[13;2u` | `shift+enter` |
| `\x1b[13;3u` | `alt+enter` |
| `\x1b[13;5u` | `ctrl+enter` |
| `\x1b[13;6u` | `ctrl+shift+enter` |
| `\x1b[13;9u` | `super+enter` |

So `shift+enter` is a real, bindable key — conditionally. Whether it arrives
depends on the terminal, which is why τ never makes it the only way to do
anything.

Terminals that implement the protocol include kitty, foot, ghostty, WezTerm and
recent Alacritty. `TERM=xterm-256color` says nothing either way: it is what most
of them set. Run the probe in §6 rather than guessing.

### A measured example

MATE Terminal 1.26.0 on VTE 0.70.6 (Debian 12), `TERM=xterm-256color`:

```
kitty query: NO REPLY
Enter        -> bytes '\r'      textual ['enter']
Ctrl+Enter   -> bytes '\r'      textual ['enter']
Shift+Enter  -> bytes '\r'      textual ['enter']
Alt+Enter    -> bytes '\x1b\r'  textual ['enter']
```

Three keys, one byte. This is the common case on the VTE family — MATE Terminal,
GNOME Terminal, Xfce Terminal — and it is not something an application can work
around, because the three keystrokes are identical by the time they leave the
terminal. On such a terminal Ctrl+J is the only newline gesture in `"submit"`
mode, which is also exactly what pi and Claude Code do there.

A later VTE release may add the protocol; this was not checked. Switching to a
terminal from the list above is the reliable fix.

## 3. Two legacy escape hatches, and why neither is one

**Alt+Enter.** Many terminals encode Alt+*key* as ESC followed by the key's
byte, so Alt+Enter is `\x1b\r`. This one really does reach the application: it is
the ONE modifier a legacy terminal can express on Enter. Textual then discards
it. `_xterm_parser.py` applies the `alt+` prefix only when the resulting key name
is a single character (`if len(name) == 1 and alt`), and `enter` is five:

```
feed('\x1b\r'), then tick() past ESCAPE_DELAY  ->  ['enter']
feed('\x1ba'),  then tick() past ESCAPE_DELAY  ->  ['alt+a']
```

So the loss is Textual's, not the terminal's, and it is narrow — it costs
`alt+enter` and `alt+tab`, while `alt+up` and every other named key with a CSI
modifier form is unaffected. Under the kitty protocol Alt+Enter arrives as
`\x1b[13;3u` and is named correctly.

τ does not work around it. Recovering the modifier means replacing a private
Textual parser function, which is a version-coupled patch on a hot path for one
keystroke that Ctrl+J already delivers. The upstream condition is where this
should be fixed.

**xterm `modifyOtherKeys`.** Ctrl+Enter can arrive as `\x1b[27;5;13~`. Textual
parses it into the key name `ctrl+\r`, which no binding will ever match. pi
handles this format explicitly (`tui/src/keys.ts:705`); Textual does not.

τ does not work around either. Both would be a second encoding to keep in step
with the first, for a keystroke Ctrl+J already delivers on every terminal ever
made.

## 4. Where the swap has to live in Textual

`ChatInput` is a `TextArea`. Three measured facts constrain the implementation.

**A `Binding("enter", …)` on a `TextArea` never fires.** `TextArea._on_key`
(`_text_area.py:1828`) claims Enter, inserts `"\n"`, and calls `event.stop()`.
Textual checks non-priority bindings at App level, once the key has bubbled all
the way up, which a stopped key does not do. Measured:

```
Plain TextArea, press enter          -> text '\n',  binding did not fire
TextArea + Binding("enter", ...)     -> text '\n',  binding did not fire
TextArea + Binding(..., priority=True) -> text '',  binding fired
```

**`priority=True` is the wrong instrument.** Priority bindings are checked
*before* the event reaches any widget (`app.py:4136`), so an app-level priority
Enter binding takes Enter away from every other editor and dialog on the screen.

**A subclass's `on_key` runs first, and can stop it.**
`MessagePump._get_dispatch_methods` walks the MRO and takes `_on_key` over
`on_key` per class, so the order is `ChatInput.on_key` then `TextArea._on_key`,
and `message._no_default_action` — what `prevent_default()` sets — ends the walk.
Measured: a subclass `on_key` that calls `prevent_default()` on Enter leaves the
document empty.

So the swap lives in `ChatInput.on_key`. That is the seam Textual provides, not
a workaround for a missing one.

## 5. The setting

`enter_key` in `~/.tau/config.json`:

- **`"newline"`** (default) — Enter inserts a line break. Ctrl+J sends.
- **`"submit"`** — Enter sends. Shift+Enter and Ctrl+J insert a line break.
  This is pi's pair: `tui/src/keybindings.ts:143` binds `tui.input.newLine` to
  `["shift+enter", "ctrl+j"]` and `tui.input.submit` to `enter`.

An unrecognised value raises `ConfigError` while τ is starting.

The Footer names whichever key sends. `Parley.check_action` keeps exactly one of
the two app-level bindings live, and a falsy `check_action` both hides a binding
and stops it consuming the key, so in `"submit"` mode Ctrl+J does not send from a
non-editor focus either.

### Why `"newline"` stays the default

Opinion, stated as such. A prompt to a coding agent is usually several lines, and
Enter is the key a text editor already spends on a line break. The two failure
modes are not symmetric: sending half a prompt cannot be undone, while an
unwanted line break costs one Backspace.

The reason the setting exists anyway is that this argument does not survive
contact with a user who moves between τ and three other agents in an afternoon.
Muscle memory is worth more than the better default.

### What τ deliberately does not do

**Backslash continuation.** Claude Code lets a line ending in `\` continue
instead of sending. pi has no such rule, and τ does not add one. It converts a
missed keystroke into a message that ends in a stray `\` — a silent corruption of
the text rather than a visible failure, which is the wrong trade under
"Fail Early".

**A runtime toggle.** `enter_key` is read from config on every keystroke, so an
edit takes effect on the next key rather than at the next restart. There is no
command or CLI flag for it: this is a preference set once, not a per-run choice.

## 6. Probing your terminal

`scripts/keyprobe.py` reports what your terminal sends and what Textual calls it.

1. Run it from a real terminal: `venv/bin/python scripts/keyprobe.py`.
2. Read the `kitty query` line. `NO REPLY` means Shift+Enter cannot reach τ on
   this terminal, and Ctrl+J is your newline key in `"submit"` mode.
3. Press Enter, Shift+Enter, Ctrl+Enter, Alt+Enter and Ctrl+J. Each line shows
   the raw bytes and the Textual key name.
4. Press Ctrl+C to stop. The probe restores the terminal on the way out.

If Shift+Enter reports `bytes '\r'`, the terminal is not sending the modifier and
no application can recover it. Switching to a terminal that speaks the kitty
protocol is the fix, not a change to τ.

The probe drives `Parser.tick()` on its read timeout, which is what resolves an
ESC-prefixed sequence. Without that call it printed an empty key list for
Alt+Enter — which reads as "nothing arrived" when the truth is that Textual
resolves the sequence and then drops the modifier (§3). A diagnostic that
under-reports is worse than none, so the flush is not optional here.
