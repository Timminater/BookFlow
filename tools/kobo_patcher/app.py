"""Four-step Windows wizard; credentials and sessions stay in memory."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from .account import AccountError, BookFlowAccount
from .device import KoboDevice, WindowsVolumeScanner, eject_volume
from .pairing import PairingCode, PairingError, parse_pairing_code
from .service import KoboPatcher, PatchError
from .workflow import pair_and_patch


class KoboPatcherApp(ttk.Frame):
    STEPS = ("Inloggen", "Kobo kiezen", "Koppelen", "Afronden")

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=24)
        self.master = master
        self.scanner = WindowsVolumeScanner()
        self.account: BookFlowAccount | None = None
        self.code: PairingCode | None = None
        self.devices: list[KoboDevice] = []
        self.selected: KoboDevice | None = None
        self.busy = False
        self.step = 0
        self.tasks = queue.Queue()
        self.server_var = tk.StringVar(value="https://")
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.code_var = tk.StringVar()
        self.device_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.step_var = tk.StringVar()
        master.title("BookFlow Kobo-patcher")
        master.geometry("700x610")
        master.minsize(640, 580)
        master.protocol("WM_DELETE_WINDOW", self.close)
        self.pack(fill="both", expand=True)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        ttk.Label(self, text="Je boeken op je Kobo", font=("Segoe UI", 20, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self.step_var, padding=(0, 10, 0, 14)).grid(row=1, column=0, sticky="w")
        ttk.Separator(self).grid(row=2, column=0, sticky="ew")
        self.body = ttk.Frame(self, padding=(0, 20))
        self.body.grid(row=3, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        ttk.Label(self, textvariable=self.status_var, wraplength=610).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self._show(0)
        self.after(100, self._poll)

    def _label(self, text: str, row: int, *, title: bool = False) -> None:
        ttk.Label(self.body, text=text, wraplength=590,
                  font=("Segoe UI", 12, "bold") if title else ("Segoe UI", 10)).grid(
            row=row, column=0, sticky="w", pady=(0, 8))

    def _button(self, text: str, command, row: int) -> ttk.Button:
        button = ttk.Button(self.body, text=text, command=command)
        button.grid(row=row, column=0, sticky="w", pady=(6, 0))
        return button

    def _clear_page(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.status_var.set("")

    def _show(self, step: int) -> None:
        self.step = step
        self.step_var.set(f"Stap {step + 1} van 4  ·  " + "  →  ".join(self.STEPS))
        self._clear_page()
        (self._login_page, self._device_page, self._confirm_page, self._finish_page)[step]()

    def _login_page(self) -> None:
        self._label("Log in met je eigen BookFlow-account", 0, title=True)
        self._label("Gebruik het adres waarop je BookFlow opent. Je wachtwoord wordt niet opgeslagen.", 1)
        fields = [("BookFlow-adres", self.server_var, False),
                  ("Gebruikersnaam", self.username_var, False), ("Wachtwoord", self.password_var, True)]
        for index, (label, variable, secret) in enumerate(fields):
            row = 2 + index * 2
            self._label(label, row)
            entry = ttk.Entry(self.body, textvariable=variable, show="•" if secret else "", width=65)
            entry.grid(row=row + 1, column=0, sticky="ew", pady=(0, 6))
            if secret:
                entry.bind("<Return>", lambda _: self.login())
        self._button("Inloggen en verder", self.login, 8)
        self._button("Externe login? Gebruik een koppelcode", self._code_page, 9)
        self._button("Kobo herstellen zonder inloggen", self._restore_page, 10)

    def login(self) -> None:
        if self.busy:
            return
        address, username, password = self.server_var.get(), self.username_var.get(), self.password_var.get()
        self.password_var.set("")

        def work():
            account = BookFlowAccount(address)
            account.login(username, password)
            return account

        def done(account):
            self.account = account
            self.code = None
            self._show(1)

        self._run(work, done, "Inloggen bij BookFlow…")

    def _code_page(self) -> None:
        self._clear_page()
        self.password_var.set("")
        self._label("Inloggen via een eenmalige koppelcode", 0, title=True)
        self._label("Log in op je BookFlow-website en open Mijn instellingen → Kobo. Genereer een koppelcode en plak die hieronder. Dit werkt ook bij externe login.", 1)
        ttk.Entry(self.body, textvariable=self.code_var, show="•", width=65).grid(row=2, column=0, sticky="ew")
        self._button("Verder", self._use_code, 3)
        self._button("Terug naar inloggen", lambda: self._show(0), 4)

    def _use_code(self) -> None:
        try:
            self.code = parse_pairing_code(self.code_var.get())
        except PairingError:
            self.status_var.set("Deze koppelcode is niet geldig. Kopieer een nieuwe code uit BookFlow.")
            return
        self.code_var.set("")
        self._show(1)

    def _device_page(self) -> None:
        name = self.account.username if self.account else "het account van je koppelcode"
        self._label(f"Kies de Kobo voor {name}", 0, title=True)
        self._label("1. Sluit je Kobo met een USB-kabel aan op deze computer.\n2. Kies Verbinden op het scherm van de Kobo.\n3. Kies hieronder de juiste reader.", 1)
        self.device_box = ttk.Combobox(self.body, textvariable=self.device_var, state="readonly", width=65)
        self.device_box.grid(row=2, column=0, sticky="ew", pady=(8, 14))
        self.device_box.bind("<<ComboboxSelected>>", self._select_device)
        self._button("Opnieuw zoeken", self.scan, 3)
        self.next_button = self._button("Deze Kobo gebruiken", lambda: self._show(2), 4)
        self.next_button.state(["disabled"])
        self._button("Terug", self._back_to_login, 5)
        self.scan()

    def scan(self) -> None:
        if self.busy:
            return

        def done(devices):
            self.devices = devices
            self.selected = None
            self.device_box["values"] = [self._device_label(device) for device in devices]
            self.device_var.set("")
            if len(devices) == 1:
                self.device_box.current(0)
                self._select_device()
                self.status_var.set("Eén Kobo gevonden. Controleer de reader en ga verder.")
            else:
                self.next_button.state(["disabled"])
                self.status_var.set("Kies de juiste Kobo uit de lijst." if devices else "Geen Kobo gevonden. Controleer de USB-kabel en kies Verbinden op je Kobo. Klik daarna op Opnieuw zoeken.")

        self._run(self.scanner.scan, done, "Zoeken naar aangesloten Kobo-readers…")

    @staticmethod
    def _device_label(device: KoboDevice) -> str:
        details = ", ".join(value for value in (device.platform, device.firmware, device.serial_masked) if value)
        return f"{device.display_name}{' (' + details + ')' if details else ''}"

    def _select_device(self, _event=None) -> None:
        index = self.device_box.current()
        self.selected = self.devices[index] if 0 <= index < len(self.devices) else None
        self.next_button.state(["!disabled"] if self.selected else ["disabled"])

    def _confirm_page(self) -> None:
        if self.selected is None:
            self._show(1)
            return
        origin = self.account.origin if self.account else self.code.origin
        name = self.account.username if self.account else "Account van de koppelcode"
        self._label("Controleer je koppeling", 0, title=True)
        self._label(f"BookFlow: {urlsplit(origin).netloc}\nAccount: {name}\nKobo: {self._device_label(self.selected)}", 1)
        self._label("We maken eerst een reservekopie van de configuratie. Daarna stellen we deze Kobo in voor jouw BookFlow-boeken. Bestaande boeken blijven staan.", 2)
        self._label("De officiële Kobo Store- en OverDrive-synchronisatie verandert hierdoor. Een bestaande BookFlow-koppeling van dit account wordt vervangen. Je kunt de oorspronkelijke configuratie later herstellen.", 3)
        self._button("Koppelen en Kobo instellen", self.patch, 4)
        self._button("Terug naar Kobo kiezen", lambda: self._show(1), 5)

    def patch(self) -> None:
        if self.busy or self.selected is None:
            return
        device, account, code = self.selected, self.account, self.code

        def work():
            current_code = account.pairing_code() if account else code
            if current_code is None:
                raise AccountError("Log opnieuw in bij BookFlow.")
            return pair_and_patch(device, current_code)

        def done(result):
            self.code = None
            if self.account:
                self.account.clear()
                self.account = None
            self._show(3)
            self.status_var.set(f"Reservekopie gemaakt: {result.backup_path.name}")

        self._run(work, done, "Koppeling maken en Kobo instellen. Laat de USB-kabel aangesloten…")

    def _finish_page(self) -> None:
        self._label("Je Kobo is gekoppeld", 0, title=True)
        self._label("1. Klik hieronder op Veilig uitwerpen.\n2. Haal daarna de USB-kabel los.\n3. Start je Kobo opnieuw op en verbind met wifi.\n4. Kies Synchroniseren op je Kobo.", 1)
        self._label("Zet in BookFlow boeken op je Kobo-sync-lijst. Die worden bij de volgende synchronisatie opgehaald.", 2)
        self._button("Veilig uitwerpen", self.eject, 3)
        self._button("Sluiten", self.close, 4)

    def _back_to_login(self) -> None:
        if self.account:
            self.account.clear()
        self.account = None
        self.code = None
        self._show(0)

    def _restore_page(self) -> None:
        self._show(1)
        self.next_button.configure(text="Herstelopties openen", command=self._restore_options)

    def _restore_options(self) -> None:
        if self.selected is None:
            return
        self._clear_page()
        self._label("Kobo herstellen", 0, title=True)
        self._label(self._device_label(self.selected), 1)
        backups = KoboPatcher(self.selected).available_backups()
        self.backup_box = ttk.Combobox(self.body, values=[str(path) for path in backups], state="readonly", width=65)
        self.backup_box.grid(row=2, column=0, sticky="ew")
        if backups:
            self.backup_box.current(0)
        self._button("Gekozen reservekopie terugzetten", self.restore_backup, 3)
        self._button("Officiële Kobo-sync herstellen", self.restore_official, 4)
        self._button("Veilig uitwerpen", self.eject, 5)
        self._button("Terug", lambda: self._show(0), 6)

    def restore_backup(self) -> None:
        if self.selected is None or not self.backup_box.get():
            self.status_var.set("Kies eerst een reservekopie.")
            return
        if messagebox.askyesno("Reservekopie terugzetten", "Wil je de gekozen configuratie terugzetten? We bewaren eerst de huidige configuratie."):
            device, backup = self.selected, Path(self.backup_box.get())
            self._run(lambda: KoboPatcher(device).restore_backup(backup),
                      lambda _: self.status_var.set("Reservekopie hersteld. Werp de Kobo nu veilig uit."), "Reservekopie herstellen…")

    def restore_official(self) -> None:
        if self.selected and messagebox.askyesno("Officiële Kobo-sync", "Wil je de officiële Kobo-synchronisatie herstellen? Dit wist je boeken niet."):
            device = self.selected
            self._run(lambda: KoboPatcher(device).restore_official_sync(),
                      lambda _: self.status_var.set("Officiële Kobo-sync hersteld. Werp de Kobo nu veilig uit."), "Kobo-sync herstellen…")

    def eject(self) -> None:
        if self.selected:
            device = self.selected
            self._run(lambda: eject_volume(device.root), lambda ok: self.status_var.set(
                "Veilig uitgeworpen. Je kunt de kabel loshalen en de Kobo opnieuw starten."
                if ok else "Windows kon de Kobo niet uitwerpen. Sluit geopende bestanden en gebruik Veilig verwijderen in Windows voordat je de kabel loshaalt."
            ), "Kobo veilig uitwerpen…")

    def _run(self, work, done, status: str) -> None:
        if self.busy:
            return
        self.busy = True
        self.status_var.set(status)
        for child in self.body.winfo_children():
            if isinstance(child, (ttk.Button, ttk.Entry, ttk.Combobox)):
                child.state(["disabled"])

        def worker():
            try:
                self.tasks.put((done, work(), None))
            except Exception as exc:
                self.tasks.put((done, None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        try:
            done, result, error = self.tasks.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for child in self.body.winfo_children():
                if isinstance(child, (ttk.Button, ttk.Entry, ttk.Combobox)):
                    child.state(["!disabled"])
            if error:
                self.status_var.set(str(error) if isinstance(error, (AccountError, PairingError, PatchError))
                                    else "Deze stap is niet gelukt. Controleer de verbinding en probeer opnieuw.")
            else:
                self.status_var.set("")
                done(result)
        self.after(100, self._poll)

    def close(self) -> None:
        if self.busy:
            messagebox.showinfo("Even wachten", "Wacht tot de huidige stap klaar is voordat je de app sluit.")
            return
        if self.account:
            self.account.clear()
        self.password_var.set("")
        self.code_var.set("")
        self.master.destroy()


def main() -> None:
    root = tk.Tk()
    KoboPatcherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
