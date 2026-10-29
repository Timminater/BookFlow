import os
import time

import pytest

from kobo_patcher.device import KoboDevice
from kobo_patcher.pairing import PairingCode
from kobo_patcher.service import PatchError
from kobo_patcher.workflow import pair_and_patch


def test_pairing_write_failure_revokes_new_token(tmp_path, monkeypatch):
    from kobo_patcher import workflow
    calls = []
    class Client:
        def __init__(self, code):
            pass
        def claim(self):
            return "one-time-session"
        def complete(self, session, label):
            return "https://books.example/api/kobo/test-token"
        def revoke(self, session):
            calls.append(session)
            return True
    monkeypatch.setattr(workflow, "PairingClient", Client)
    monkeypatch.setattr(workflow.KoboPatcher, "current_endpoint", lambda _: None)
    def fail(*args):
        raise PatchError("write failed")
    monkeypatch.setattr(workflow.KoboPatcher, "patch", fail)
    with pytest.raises(PatchError, match="write failed"):
        pair_and_patch(KoboDevice(tmp_path, "Glo", None, None, None), PairingCode("", "https://books.example", "x", 9999999999, 1))
    assert calls == ["one-time-session"]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows/Tk wizard test")
def test_wizard_requires_login_and_device_selection_and_reaches_finish(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    from kobo_patcher import app
    device = KoboDevice(tmp_path, "Kobo Glo", "4.38.23684", "N613", "123…")
    monkeypatch.setattr(app.WindowsVolumeScanner, "scan", lambda _: [device, device])
    root = tk.Tk()
    root.withdraw()
    wizard = app.KoboPatcherApp(root)
    def pump():
        deadline = time.monotonic() + 3
        while wizard.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        root.update_idletasks()
        assert not wizard.busy
    try:
        assert wizard.step == 0
        assert any(isinstance(w, ttk.Entry) and w.cget("show") == "•" for w in wizard.body.winfo_children())
        class Account:
            username = "Marion"
            origin = "https://books.example"
            def __init__(self, origin):
                pass
            def login(self, user, password):
                assert user == "Marion"
                assert password == "not-stored"
            def pairing_code(self):
                return PairingCode("", self.origin, "x", 9999999999, 1)
            def clear(self):
                self.username = None
        monkeypatch.setattr(app, "BookFlowAccount", Account)
        wizard.server_var.set("https://books.example")
        wizard.username_var.set("Marion")
        wizard.password_var.set("not-stored")
        wizard.login()
        pump()
        assert wizard.password_var.get() == ""
        assert wizard.step == 1
        assert wizard.selected is None
        assert "disabled" in wizard.next_button.state()
        wizard.device_box.current(1)
        wizard._select_device()
        wizard.next_button.invoke()
        assert wizard.step == 2
        class Result:
            backup_path = tmp_path / "config.backup"
        monkeypatch.setattr(app, "pair_and_patch", lambda selected, code: Result())
        wizard.patch()
        pump()
        assert wizard.step == 3
        assert wizard.account is None
        assert wizard.code is None
        assert "Reservekopie gemaakt" in wizard.status_var.get()
    finally:
        root.destroy()
