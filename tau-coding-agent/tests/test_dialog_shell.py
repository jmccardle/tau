"""Every modal is built from one shell, and states the two things that differ.

Reference: docs/TUI-STYLE-GUIDE.md §1.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tau_coding_agent import dialogs, extension_ui, modals, session_picker, tree_browser

STYLESHEET = Path(dialogs.__file__).with_name("tau.tcss")


def _dialog_classes():
    """Every concrete :class:`TauDialog` subclass in the TUI."""
    found = []
    for module in (modals, tree_browser, session_picker, extension_ui):
        for name in dir(module):
            obj = getattr(module, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, dialogs.TauDialog)
                and obj not in (
                    dialogs.TauDialog,
                    dialogs.ChoiceDialog,
                    dialogs.TextDialog,
                    modals._FieldForm,
                )
            ):
                found.append(obj)
    return sorted(set(found), key=lambda cls: cls.__name__)


def test_there_are_dialogs_to_check():
    assert len(_dialog_classes()) >= 9


@pytest.mark.parametrize("cls", _dialog_classes(), ids=lambda c: c.__name__)
def test_every_dialog_declares_its_own_frame_id(cls):
    """A dialog that forgets DIALOG_ID has no id for its sizing rule to match.

    That is not a styling nit: `#tree-browser-dialog` disappeared from the DOM the
    first time this shell was applied, and the failure surfaced four files away.
    """
    assert cls.DIALOG_ID != dialogs.TauDialog.DIALOG_ID, (
        f"{cls.__name__} inherits the placeholder DIALOG_ID"
    )


@pytest.mark.parametrize("cls", _dialog_classes(), ids=lambda c: c.__name__)
def test_every_frame_id_is_sized_by_the_stylesheet(cls):
    """The id exists to carry width and height; an unsized frame is a dead id."""
    assert f"#{cls.DIALOG_ID}" in STYLESHEET.read_text()


@pytest.mark.parametrize("cls", _dialog_classes(), ids=lambda c: c.__name__)
def test_no_dialog_rewrites_the_shell(cls):
    """`compose` belongs to the shell; a subclass supplies `compose_body`."""
    assert "compose" not in vars(cls), f"{cls.__name__} overrides compose()"


def test_the_shell_owns_the_escape_binding():
    """One binding, inherited, rather than one per screen."""
    keys = {
        binding[0] if isinstance(binding, tuple) else binding.key
        for binding in dialogs.TauDialog.BINDINGS
    }
    assert "escape" in keys


def test_a_cancelled_dialog_fabricates_no_answer():
    """Fail-Early, stated as a class attribute rather than an `action_cancel` body.

    ``ExtensionConfirmModal``/``ExtensionSelectModal`` used to make this point;
    both are gone (docs/EXTENSION-LOCKS.md §8.2) and the two that carry an answer
    make it now — a dismissed form and a dismissed ask each answer ``None``.
    """
    assert modals.ExtensionFormScreen.CANCEL_VALUE is None
    assert extension_ui.ExtensionAskScreen.CANCEL_VALUE is None


def test_choice_values_are_declared_beside_their_buttons():
    """The mapping is stated once; nothing rebuilds it in an event handler."""
    assert dict((bid, value) for _label, bid, value in tree_browser.TreeModeModal.CHOICES) == {
        "mode-navigate": "navigate",
        "mode-summarize": "summarize",
        "mode-custom": "custom",
        "mode-cancel": None,
    }


def test_the_stylesheet_states_each_dialog_role_once():
    """The four role rules replaced seven copies each; they stay single."""
    css = re.sub(r"/\*.*?\*/", "", STYLESHEET.read_text(), flags=re.S)
    for role in ("tau-dialog-title", "tau-dialog-buttons", "tau-dialog-input", "tau-dialog-hint"):
        selectors = re.findall(rf"^\.{role}\s*\{{", css, flags=re.M)
        assert len(selectors) == 1, f".{role} is declared {len(selectors)} times"


def test_the_stylesheet_carries_no_multi_line_comment():
    """Prose lives in docs/TUI-STYLE-GUIDE.md §5, for the reason `cf3e30f` gives.

    A run of comment lines is prose that outgrew its place, and it drifts silently
    because nothing checks it. A one-line comment stating a dependence is fine.
    """
    offenders = [
        " ".join(block.split())[:70]
        for block in re.findall(r"/\*.*?\*/", STYLESHEET.read_text(), flags=re.S)
        if len(block.splitlines()) > 1
    ]
    assert offenders == [], offenders


def test_the_stylesheet_cites_the_guide():
    """The prose moved; the pointer to where has to stay."""
    assert "docs/TUI-STYLE-GUIDE.md" in STYLESHEET.read_text()
