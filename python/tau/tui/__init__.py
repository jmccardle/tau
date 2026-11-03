"""The terminal UI (D23): `TauApp`, a Textual app over a `Client`, and `SessionView`, the picture
of the session it draws from.

`tau` with no `-p` opens it. Embed it with `TauApp(client, cursor, welcome=..., home=...)` and
`await app.run_async()`; test one with `app.run_test()`. Importing this module imports Textual,
which `tau -p` never does. `SessionView` needs no screen: feed it a client's events.
"""

from tau.tui._app import TauApp
from tau.tui._view import FormingCall, LiveLayer, SessionView

__all__ = ["FormingCall", "LiveLayer", "SessionView", "TauApp"]
