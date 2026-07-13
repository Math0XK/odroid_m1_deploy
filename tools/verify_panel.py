#!/usr/bin/env python3
"""
verify_panel.py — onglet Vérification post-déploiement (GO/NO-GO) du tableau de
bord (`station.py`) : pertinent après un flash SPI COMME après un clonage NVMe,
pas rattaché à un seul des deux.

Réutilise `check_deploy.run_checks` — même logique que la sous-commande headless
`station.py check` (utilisable en SSH quand le flash s'est fait depuis un autre
poste). `VerifyPanel` est un `ttk.Frame` embarquable.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext

import check_deploy


class VerifyPanel(ttk.Frame):
    """Vérification post-déploiement GO/NO-GO, embarquable dans n'importe quel
    conteneur tkinter (`master`)."""

    def __init__(self, master):
        super().__init__(master)
        self._ui_queue = queue.Queue()
        self._busy = False
        self._build_ui()
        self.after(100, self._pump_ui_queue)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 5}
        btn = {"padx": 6, "pady": 4}

        v_frame = ttk.LabelFrame(self, text="Vérification post-déploiement (SUR l'unité)")
        v_frame.pack(fill="x", **pad)
        ttk.Label(v_frame, wraplength=780, justify="left",
                  text="Contrôle GO/NO-GO : kernel vendor, racine sur NVMe, dmesg "
                       "sans erreur NPU connue, nœuds DRI présents, et — en "
                       "option — un smoke test d'inférence NPU.").pack(anchor="w", padx=4)
        vrow = ttk.Frame(v_frame); vrow.pack(fill="x")
        ttk.Label(vrow, text="Test NPU (optionnel) :").pack(side="left", padx=4)
        self.npu_entry = ttk.Entry(vrow, width=52)
        self.npu_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.verify_btn = ttk.Button(v_frame, text="Vérifier ce déploiement (GO / NO-GO)",
                                     command=self._on_verify)
        self.verify_btn.pack(side="left", **btn)

        self.verdict_label = ttk.Label(self, text="", font=("TkDefaultFont", 14, "bold"))
        self.verdict_label.pack(fill="x", padx=8, pady=(4, 0))

        self.activity = ttk.Progressbar(self, mode="indeterminate")
        self.activity.pack(fill="x", padx=8, pady=(4, 0))
        log_frame = ttk.LabelFrame(self, text="Journal")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(log_frame, height=14, state="disabled")
        self.log.pack(fill="both", expand=True)

    # ---------- Passerelle thread de travail -> UI ----------
    def _pump_ui_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert(tk.END, payload + "\n")
                    self.log.see(tk.END)
                    self.log.configure(state="disabled")
                elif kind == "verdict":
                    go, text = payload
                    self.verdict_label.config(
                        text=text, foreground=("green" if go else "red"))
                elif kind == "done":
                    self._busy_stop()
        except queue.Empty:
            pass
        self.after(100, self._pump_ui_queue)

    def log_write(self, text):
        self._ui_queue.put(("log", text))

    # ---------- État occupé ----------
    def _busy_start(self):
        self._busy = True
        self.verify_btn.config(state="disabled")
        self.activity.start(12)

    def _busy_stop(self):
        self.activity.stop()
        self.verify_btn.config(state="normal")
        self._busy = False

    # ---------- Vérif déploiement ----------
    def _on_verify(self):
        if self._busy:
            return
        npu = self.npu_entry.get().strip() or None
        self.verdict_label.config(text="")
        self.log_write("\n--- Vérification post-déploiement (sur cette unité) ---")
        self._busy_start()
        threading.Thread(target=self._guard(self._verify_worker), args=(npu,),
                         daemon=True).start()

    def _guard(self, target):
        def wrapped(*args):
            try:
                target(*args)
            except Exception as e:                       # filet : jamais de thread muet
                self.log_write(f"\n=== ERREUR ===\n{e}")
                self._ui_queue.put(("verdict", (False, f"ERREUR : {e}")))
                self._ui_queue.put(("done", None))
        return wrapped

    def _verify_worker(self, npu_cmd):
        results, go = check_deploy.run_checks(npu_cmd=npu_cmd)
        for name, ok, msg in results:
            self.log_write(f"[{'OK ' if ok else 'NON'}] {name} : {msg}")
        verdict = "GO ✔" if go else "NO-GO ✖"
        self.log_write(f"=== {verdict} ===")
        self._ui_queue.put(("verdict", (go, verdict)))
        self._ui_queue.put(("done", None))
