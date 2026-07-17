#!/usr/bin/env python3
"""
spi_panel.py — onglet SPI du tableau de bord (`station.py`) : sauvegarde du
« golden », flash de la flotte, variables d'env U-Boot.

Contexte : sur ce projet le RK3568 boote son bootloader (idbloader-spi + u-boot
Armbian + env) **depuis la puce SPI 16 MiO**, et le NVMe boote seul derrière (voir
../docs/DEPLOIEMENT_FLOTTE.md). Toute cette config vit aujourd'hui sur un unique
master physique -> cet onglet la sauvegarde durablement (sous `../images/spi/`,
versionné) et la reflashe sur les autres cartes.

`SpiPanel` est un `ttk.Frame` embarquable, monté comme onglet par `station.py`
(entrée unique GUI + CLI). Les OPÉRATIONS réelles (flashrom, fw_setenv,
garde-fous) vivent dans `spi_ops.SpiOps`, partagé avec les sous-commandes CLI
`station.py spi …` — même code, mêmes garde-fous des deux côtés. La logique pure
(validation d'image, parsers) est dans `spi_core` (testée sans matériel).
L'affichage suit la même refonte que l'onglet Clone : bandeau d'état + journal
en couleurs (`ui_widgets`), niveaux du `report.Reporter`.

Méthode de l'outil : à la PINCE CH341A (clip SOIC-8), carte HORS TENSION
(`flashrom -p ch341a_spi`) — marche même sur une unité vierge/briquée. Le flash
SPI depuis Linux n'est pas possible (le SFC du RK3568 n'expose pas la puce
entière) ; sans pince, flasher depuis le prompt U-Boot avec `sf write` (voir
docs/DEPLOIEMENT_FLOTTE.md §5).

Case « Mode simulation » : n'exécute RIEN, journalise seulement les commandes
flashrom/fw_setenv exactes (revue de sécurité, sans matériel).
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from report import Reporter
from spi_ops import PROGRAMMER, SpiOps
from ui_widgets import LogView, ScrollableFrame, StatusBanner, hint, scroll_height, section


class SpiPanel(ttk.Frame):
    """Panel Golden / Flash flotte / Env U-Boot — embarquable dans n'importe quel
    conteneur tkinter (`master`) : `station.py` le monte comme onglet."""

    def __init__(self, master):
        super().__init__(master)

        self._ui_queue = queue.Queue()
        self._busy = False

        # Paquet autonome : tools/ -> racine du paquet = parent ; images/spi/ à côté.
        self._pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._golden_dir = os.path.join(self._pkg_root, "images", "spi")
        default_golden = os.path.join(self._golden_dir, "golden_spi_16MiB.bin")

        self._build_ui(default_golden)
        self.after(100, self._pump_ui_queue)

    # ---------- UI ----------
    def _build_ui(self, default_golden):
        btn = {"padx": 6, "pady": 4}

        # Contrôles de config (1/2/3) : zone défilante de hauteur fixe, pour que
        # le bandeau/la progression/le journal packés après restent TOUJOURS
        # visibles, même sur un écran bas.
        scroll = ScrollableFrame(self, height=scroll_height(self))
        scroll.pack(side="top", fill="x")
        body = scroll.body

        # --- 1 · Golden ---
        g_frame = section(body, "1 · Golden — lire / vérifier la puce (16 MiO, pince CH341A)")
        row = ttk.Frame(g_frame); row.pack(fill="x", padx=4)
        ttk.Label(row, text="Fichier :").pack(side="left", padx=4)
        self.golden_entry = ttk.Entry(row)
        self.golden_entry.insert(0, default_golden)
        self.golden_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="Parcourir…", command=self._browse_golden).pack(side="left", padx=4)
        brow = ttk.Frame(g_frame); brow.pack(fill="x")
        ttk.Button(brow, text="Lire la puce → golden",
                   command=self._on_read_master).pack(side="left", **btn)
        ttk.Button(brow, text="Vérifier la puce vs golden",
                   command=self._on_verify_chip).pack(side="left", **btn)
        hint(g_frame, "Pince CH341A (clip SOIC-8), carte HORS TENSION. Sans "
                      "pince : flasher au prompt U-Boot avec « sf write » "
                      "(docs/DEPLOIEMENT_FLOTTE.md §5) — le flash SPI depuis "
                      "Linux n'est pas possible sur cette carte.")

        # --- 2 · Flash unité ---
        f_frame = section(body, "2 · Flasher une unité (EFFACE sa puce SPI)")
        ttk.Button(f_frame, text="Flasher cette unité avec le golden",
                   command=self._on_flash_unit).pack(side="left", **btn)
        hint(f_frame, "Sauvegarde la puce cible AVANT le flash "
                      "(preflash_backups/), contrôle le golden (taille + "
                      "signature + SHA256), écrit puis vérifie.")

        # --- 3 · Env U-Boot ---
        e_frame = section(body, "3 · Variables d'env U-Boot (sur l'unité, root)")
        erow = ttk.Frame(e_frame); erow.pack(fill="x")
        ttk.Button(erow, text="Appliquer les 4 vars d'env",
                   command=self._on_env_apply).pack(side="left", **btn)
        ttk.Button(erow, text="Sauver l'env (fw_printenv)",
                   command=self._on_env_dump).pack(side="left", **btn)
        hint(e_frame, "Ré-applique les 4 variables critiques (boot NVMe + "
                      "régulateur NPU). Inutile après un flash d'image "
                      "COMPLÈTE (env inclus) ; utile après un reflash de "
                      "l'env d'origine.")

        # --- Simulation + état + journal ---
        self.var_sim = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, variable=self.var_sim,
                        text="Mode simulation — n'exécute rien, journalise les "
                             "commandes exactes").pack(anchor="w", padx=12)

        self.banner = StatusBanner(self)
        self.banner.pack(fill="x", padx=10, pady=(4, 0))
        self.activity = ttk.Progressbar(self, mode="indeterminate")
        self.activity.pack(fill="x", padx=10, pady=(4, 0))

        log_frame = ttk.LabelFrame(self, text=" Journal détaillé ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log = LogView(log_frame, height=12)
        self.log.pack(fill="both", expand=True)
        self._action_buttons = self._collect_buttons(self)

    def _collect_buttons(self, widget):
        found = []
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                found.append(child)
            found.extend(self._collect_buttons(child))
        return found

    def _browse_golden(self):
        path = filedialog.askopenfilename(
            initialdir=self._golden_dir,
            filetypes=[("Image SPI", "*.bin *.img"), ("Tous", "*.*")])
        if path:
            self.golden_entry.delete(0, tk.END)
            self.golden_entry.insert(0, path)

    # ---------- Passerelle thread de travail -> UI ----------
    def _pump_ui_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self.log.write(*payload)
                elif kind == "done":
                    self._busy_stop()
                    self.banner.set_state("ok", "Terminé ✔")
                    if payload:
                        messagebox.showinfo("Terminé", payload)
                elif kind == "error":
                    self._busy_stop()
                    self.banner.set_state("error", "Échec — voir le journal")
                    messagebox.showerror("Erreur", payload)
        except queue.Empty:
            pass
        self.after(100, self._pump_ui_queue)

    # ---------- État occupé ----------
    def _busy_start(self, title):
        self._busy = True
        self.banner.set_state("busy", title)
        for b in self._action_buttons:
            b.config(state="disabled")
        self.activity.start(12)

    def _busy_stop(self):
        self.activity.stop()
        for b in self._action_buttons:
            b.config(state="normal")
        self._busy = False

    def _start(self, title, target, *args):
        """Lance une action en thread (si pas déjà occupé)."""
        if self._busy:
            return
        self.log.write("step", title)
        self._busy_start(title)
        threading.Thread(target=self._guard(target), args=args, daemon=True).start()

    def _guard(self, target):
        def wrapped(*args):
            try:
                target(*args)
            except Exception as e:                       # filet : jamais de thread muet
                self._ui_queue.put(("log", ("error", str(e))))
                self._ui_queue.put(("error", str(e)))
        return wrapped

    def _ops(self):
        """SpiOps frais, avec l'état simulation capturé au lancement de l'action."""
        reporter = Reporter(
            sink=lambda level, text: self._ui_queue.put(("log", (level, text))))
        return SpiOps(reporter, golden_dir=self._golden_dir,
                      sim=self.var_sim.get())

    # ---------- Golden : lecture / vérif ----------
    def _on_read_master(self):
        golden = self.golden_entry.get().strip()
        if not golden:
            messagebox.showerror("Erreur", "Renseigne le chemin du golden.")
            return
        self._start(f"Lecture de la puce ({PROGRAMMER})",
                    self._read_master_worker, self._ops(), PROGRAMMER, golden)

    def _read_master_worker(self, ops, programmer, golden):
        digest = ops.read_golden(programmer, golden)
        if digest is None:
            self._ui_queue.put(("done", "Simulation : lecture non effectuée."))
        else:
            self._ui_queue.put(("done", f"Golden sauvegardé et vérifié.\n{golden}"))

    def _on_verify_chip(self):
        golden = self.golden_entry.get().strip()
        if not os.path.isfile(golden) and not self.var_sim.get():
            messagebox.showerror("Erreur", f"Golden absent : {golden}")
            return
        self._start(f"Vérification puce vs golden ({PROGRAMMER})",
                    self._verify_chip_worker, self._ops(), PROGRAMMER, golden)

    def _verify_chip_worker(self, ops, programmer, golden):
        same = ops.verify_chip(programmer, golden)
        if same is None:
            self._ui_queue.put(("done", "Simulation : vérif non effectuée."))
        elif same:
            self._ui_queue.put(("log", ("ok", "Puce IDENTIQUE au golden.")))
            self._ui_queue.put(("done", "Puce IDENTIQUE au golden ✔"))
        else:
            self._ui_queue.put(("error", "La puce DIFFÈRE du golden (voir journal)."))

    # ---------- Flash d'une unité ----------
    def _on_flash_unit(self):
        programmer = PROGRAMMER
        golden = self.golden_entry.get().strip()
        if not self.var_sim.get():
            if not os.path.isfile(golden):
                messagebox.showerror("Erreur", f"Golden absent : {golden}")
                return
            if not messagebox.askyesno(
                    "Confirmation",
                    f"Ceci EFFACE la SPI de l'unité via « {programmer} » et y écrit "
                    f"le golden.\n\nUne sauvegarde de la puce est faite avant.\n\n"
                    "Continuer ?"):
                return
        self._start(f"Flash de l'unité ({programmer})",
                    self._flash_unit_worker, self._ops(), programmer, golden)

    def _flash_unit_worker(self, ops, programmer, golden):
        backup = ops.flash_unit(programmer, golden)
        if backup is None:
            self._ui_queue.put(("done", "Simulation : flash non effectué."))
        else:
            self._ui_queue.put(("log", ("ok", f"Sauvegarde pré-flash : {backup}")))
            self._ui_queue.put(("done", "Flash réussi et vérifié.\nSauvegarde "
                                        f"pré-flash : {backup}"))

    # ---------- Env U-Boot ----------
    def _on_env_apply(self):
        if not self.var_sim.get() and not messagebox.askyesno(
                "Confirmation", "Écrire les 4 variables d'env U-Boot (mtd1) via "
                "fw_setenv ?"):
            return
        self._start("Application des 4 vars d'env U-Boot",
                    self._env_apply_worker, self._ops())

    def _env_apply_worker(self, ops):
        ops.env_apply()
        self._ui_queue.put(("done", "Simulation : env non modifié." if ops.sim
                            else "4 variables d'env appliquées (mtd1)."))

    def _on_env_dump(self):
        self._start("Sauvegarde de l'env U-Boot (fw_printenv)",
                    self._env_dump_worker, self._ops())

    def _env_dump_worker(self, ops):
        path = ops.env_dump()
        self._ui_queue.put(("done", "Simulation : env non lu." if path is None
                            else f"Env sauvegardé : {path}"))
