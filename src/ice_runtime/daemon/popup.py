from __future__ import annotations
# src/ice_studio/daemon/popup.py

import json
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List

import urllib.request
import urllib.error

DAEMON_BASE = "http://127.0.0.1:7030"


def _get_requests() -> List[Dict[str, Any]]:
    try:
        req = urllib.request.Request(f"{DAEMON_BASE}/daemon/pairing/requests")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status != 200:
                return []
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return data.get("requests", [])
    except Exception:
        return []


def _approve_request(request_id: str) -> Dict[str, Any] | None:
    try:
        payload = json.dumps({"request_id": request_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{DAEMON_BASE}/daemon/pairing/approve",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return json.loads(body)
    except Exception:
        return None


def _dismiss_request(request_id: str) -> Dict[str, Any] | None:
    try:
        payload = json.dumps({"request_id": request_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{DAEMON_BASE}/daemon/pairing/dismiss",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return json.loads(body)
    except Exception:
        return None


class PairingPopup:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("ICE Studio – Pairing")
        self.root.resizable(False, False)
        # sempre in primo piano (ma senza rubare il focus in modo aggressivo)
        self.root.attributes("-topmost", True)

        self.request_id: str | None = None

        # UI base
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")

        title = ttk.Label(frame, text="Incoming ICE flake", font=("Helvetica", 12, "bold"))
        title.grid(row=0, column=0, columnspan=2, sticky="w")

        self.info_label = ttk.Label(
            frame,
            text="Listening for pairing requests…",
            justify="left",
            wraplength=320,
        )
        self.info_label.grid(row=1, column=0, columnspan=2, pady=(8, 8), sticky="w")

        self.status_label = ttk.Label(frame, text="", foreground="#40b37a")
        self.status_label.grid(row=2, column=0, columnspan=2, sticky="w")

        self.btn_ignore = ttk.Button(frame, text="Ignore", command=self._on_ignore)
        self.btn_ignore.grid(row=3, column=0, pady=(10, 0), sticky="e")

        self.btn_accept = ttk.Button(frame, text="Accept flake", command=self._on_accept)
        self.btn_accept.grid(row=3, column=1, pady=(10, 0), sticky="w")

        # stato iniziale: nessuna richiesta
        self._set_idle()
        # all'avvio tieni nascosta la finestra
        self.root.withdraw()

        # scheduling polling
        self._schedule_poll()

    # ------------------ UI helpers ------------------

    def _set_idle(self) -> None:
        self.request_id = None
        self.info_label.config(text="Listening for pairing requests…")
        self.status_label.config(text="")
        self.btn_accept.config(state="disabled")
        self.btn_ignore.config(state="disabled")
        # nessuna richiesta → finestra nascosta
        self.root.withdraw()

    def _set_request(self, r: Dict[str, Any]) -> None:
        self.request_id = r.get("request_id")
        client_ip = r.get("client_ip") or "unknown"
        age = r.get("age_sec") or 0
        txt = (
            f"New flake pairing request:\n\n"
            f"Client: {client_ip}\n"
            f"Request ID: {self.request_id}\n"
            f"Age: {age}s"
        )
        self.info_label.config(text=txt)
        self.status_label.config(text="")
        self.btn_accept.config(state="normal")
        self.btn_ignore.config(state="normal")

        # mostra il popup SOLO quando c'e una richiesta
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

    # ------------------ polling ------------------

    def _schedule_poll(self) -> None:
        # usa un thread per non bloccare la UI
        threading.Thread(target=self._poll_once, daemon=True).start()
        # ripeti ogni 3 secondi
        self.root.after(3000, self._schedule_poll)

    def _poll_once(self) -> None:
        # prendi solo le richieste PENDING
        reqs = [r for r in _get_requests() if r.get("status") == "pending"]
        if not reqs:
            # nessuna richiesta → torna idle (solo se non stai già processando qualcosa)
            if self.request_id is None:
                self.root.after(0, self._set_idle)
            return

        # per v0 gestiamo solo la prima request
        r = reqs[0]
        # se è una nuova request, aggiorna la UI
        if r.get("request_id") != self.request_id:
            self.root.after(0, lambda: self._set_request(r))

    # ------------------ actions ------------------

    def _on_ignore(self) -> None:
        if not self.request_id:
            self._set_idle()
            return

        rid = self.request_id

        def _do():
            _dismiss_request(rid)
            self.root.after(0, self._set_idle)

        threading.Thread(target=_do, daemon=True).start()

    def _on_accept(self) -> None:
        if not self.request_id:
            return

        rid = self.request_id

        def _do():
            self.status_label.config(text="Approving flake…")
            res = _approve_request(rid)
            if res and res.get("ok") and res.get("status") == "approved":
                self.status_label.config(
                    text="❄ Flake added – this host is now trusted by ICE Studio."
                )
                # dopo un po' torna idle e nasconde la finestra
                self.root.after(1500, self._set_idle)
            else:
                self.status_label.config(text="Failed to approve flake.")

        threading.Thread(target=_do, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()


def run_popup() -> None:
    app = PairingPopup()
    app.run()
