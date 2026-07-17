#!/usr/bin/env python3
"""
verify_panel.py — onglet Vérification post-déploiement (GO/NO-GO) du tableau de
bord (`station.py`) : pertinent après un flash SPI COMME après un clonage NVMe,
pas rattaché à un seul des deux.

Réutilise `check_deploy.run_checks` — même logique que la sous-commande headless
`station.py check` (utilisable en SSH quand le flash s'est fait depuis un autre
poste). `VerifyPanel` est un `ttk.Frame` embarquable. Affichage refondu comme
les autres onglets : bandeau d'état + journal en couleurs (`ui_widgets`), un
contrôle par ligne (vert = OK, rouge = échec).
"""

import queue
import threading
from tkinter import ttk

import check_deploy
from ui_widgets import LogView, ScrollableFrame, StatusBanner, hint, scroll_height, section


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
        # Contrôles de config : zone défilante de hauteur fixe, pour que le
        # bandeau/la progression/le journal packés après restent TOUJOURS
        # visibles, même sur un écran bas — cohérent avec les autres onglets
        # même si ce panel est léger (robustesse sur un écran encore plus petit).
        scroll = ScrollableFrame(self, height=scroll_height(self))
        scroll.pack(side="top", fill="x")
        body = scroll.body

        v_frame = section(body, "Vérification post-déploiement — À LANCER SUR L'UNITÉ")
        hint(v_frame, "Contrôle GO/NO-GO : kernel vendor attendu, racine sur "
                      "NVMe, dmesg sans erreur NPU connue, nœuds DRI présents, "
                      "et — en option — un smoke test d'inférence NPU.")
        vrow = ttk.Frame(v_frame); vrow.pack(fill="x")
        ttk.Label(vrow, text="Test NPU (optionnel) :").pack(side="left", padx=4)
        self.npu_entry = ttk.Entry(vrow)
        self.npu_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.verify_btn = ttk.Button(v_frame, text="▶  Vérifier ce déploiement (GO / NO-GO)",
                                     command=self._on_verify)
        self.verify_btn.pack(side="left", padx=6, pady=6)

        self.banner = StatusBanner(self)
        self.banner.pack(fill="x", padx=10, pady=(4, 0))
        self.activity = ttk.Progressbar(self, mode="indeterminate")
        self.activity.pack(fill="x", padx=10, pady=(4, 0))

        log_frame = ttk.LabelFrame(self, text=" Détail des contrôles ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log = LogView(log_frame, height=14)
        self.log.pack(fill="both", expand=True)

    # ---------- Passerelle thread de travail -> UI ----------
    def _pump_ui_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self.log.write(*payload)
                elif kind == "verdict":
                    go, text = payload
                    self.banner.set_state("ok" if go else "error", text)
                elif kind == "done":
                    self._busy_stop()
        except queue.Empty:
            pass
        self.after(100, self._pump_ui_queue)

    # ---------- État occupé ----------
    def _busy_start(self):
        self._busy = True
        self.banner.set_state("busy", "Vérification en cours…")
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
        self.log.clear()
        self.log.write("step", "Vérification post-déploiement (sur cette unité)")
        self._busy_start()
        threading.Thread(target=self._guard(self._verify_worker), args=(npu,),
                         daemon=True).start()

    def _guard(self, target):
        def wrapped(*args):
            try:
                target(*args)
            except Exception as e:                       # filet : jamais de thread muet
                self._ui_queue.put(("log", ("error", str(e))))
                self._ui_queue.put(("verdict", (False, f"ERREUR : {e}")))
                self._ui_queue.put(("done", None))
        return wrapped

    def _verify_worker(self, npu_cmd):
        results, go = check_deploy.run_checks(npu_cmd=npu_cmd)
        for name, ok, msg in results:
            self._ui_queue.put(("log", ("ok" if ok else "error",
                                        f"{name} : {msg}")))
        verdict = "GO ✔ — unité déployable" if go else "NO-GO ✖ — voir les contrôles en échec"
        self._ui_queue.put(("log", ("step", f"Verdict : {'GO' if go else 'NO-GO'}")))
        self._ui_queue.put(("verdict", (go, verdict)))
        self._ui_queue.put(("done", None))
