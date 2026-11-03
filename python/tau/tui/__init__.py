"""The terminal UI (D23): `TauApp`, a Textual app over a `Client`, and `SessionView`, the picture
of the session it draws from.

`tau` with no `-p` opens it. Embed it with `TauApp(client, cursor, welcome=..., home=...)` and
`await app.run_async()`; test one with `app.run_test()`. Importing this module imports Textual,
which `tau -p` never does. `SessionView` needs no screen: feed it a client's events. A person who
leaves for another session (Ctrl+O, Ctrl+N) leaves `TauApp.switch` set: the caller starts it.
`SessionPicker` is the dialog that lists saved sessions.
"""

from tau.tui._app import TauApp
from tau.tui._dialogs import TauDialog
from tau.tui._picker import Holder, SessionPicker
from tau.tui._switch import Switch
from tau.tui._view import FormingCall, LiveLayer, SessionView

__all__ = [
    "FormingCall",
    "Holder",
    "LiveLayer",
    "SessionPicker",
    "SessionView",
    "Switch",
    "TauApp",
    "TauDialog",
]
