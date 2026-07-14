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

Deux méthodes d'accès à la puce, via les boutons radio en haut de l'onglet :
  - « Pince CH341A » : à la PINCE (clip SOIC), carte hors tension — marche même
    sur une unité vierge/briquée. C'est la méthode de lecture du master.
  - « Cette machine » : la puce SPI embarquée de l'Odroid où tourne la station,
    lue/vérifiée/reflashée à CHAUD par réassemblage des partitions MTD
    (`/proc/mtd` + `/dev/mtdN`) — PAS via flashrom (`internal` n'existe pas sur
    cette carte). Voir `spi_ops.SpiOps` pour le détail.

Case « Mode simulation » : n'exécute RIEN, journalise seulement les commandes
exactes (revue de sécurité, sans matériel).
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from spi_ops import MTD_PROGRAMMER, PROGRAMMERS, SpiOps, human_programmer


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
        pad = {"padx": 8, "pady": 5}
        btn = {"padx": 6, "pady": 4}

        # --- Méthode d'accès à la puce (partagée lecture/flash/vérif) ---
        m_frame = ttk.LabelFrame(self, text="Méthode d'accès à la puce SPI")
        m_frame.pack(fill="x", **pad)
        self.method_var = tk.StringVar(value=next(iter(PROGRAMMERS.values())))
        for label, val in PROGRAMMERS.items():
            ttk.Radiobutton(m_frame, text=label, value=val,
                            variable=self.method_var,
                            command=self._on_method_change).pack(anchor="w", padx=8)
        self.method_info = ttk.Label(m_frame, wraplength=820, justify="left",
                                     foreground="#666")
        self.method_info.pack(anchor="w", padx=26, pady=(2, 4))
        self.var_sim = tk.BooleanVar(value=False)
        ttk.Checkbutton(m_frame, variable=self.var_sim,
                        text="Mode simulation (n'exécute rien, journalise les "
                             "commandes)").pack(anchor="w", padx=8, pady=(0, 4))
        self._on_method_change()

        # --- 1) Golden ---
        g_frame = ttk.LabelFrame(self, text="1) Golden — lire / vérifier (16 MiO)")
        g_frame.pack(fill="x", **pad)
        row = ttk.Frame(g_frame); row.pack(fill="x")
        ttk.Label(row, text="Fichier :").pack(side="left", padx=4)
        self.golden_entry = ttk.Entry(row, width=58)
        self.golden_entry.insert(0, default_golden)
        self.golden_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="Parcourir…", command=self._browse_golden).pack(side="left", padx=4)
        brow = ttk.Frame(g_frame); brow.pack(fill="x")
        ttk.Button(brow, text="Lire la puce → golden",
                   command=self._on_read_master).pack(side="left", **btn)
        ttk.Button(brow, text="Vérifier la puce vs golden",
                   command=self._on_verify_chip).pack(side="left", **btn)

        # --- 2) Flash unité ---
        f_frame = ttk.LabelFrame(self, text="2) Flasher une unité (EFFACE la SPI)")
        f_frame.pack(fill="x", **pad)
        ttk.Label(f_frame, wraplength=780, justify="left",
                  text="Sauvegarde la puce cible AVANT le flash, contrôle le golden "
                       "(taille + signature + SHA256), puis écrit et vérifie.").pack(
            anchor="w", padx=4)
        ttk.Button(f_frame, text="Flasher cette unité avec le golden",
                   command=self._on_flash_unit).pack(side="left", **btn)

        # --- 3) Env U-Boot ---
        e_frame = ttk.LabelFrame(self, text="3) Variables d'env U-Boot (on-device, root)")
        e_frame.pack(fill="x", **pad)
        ttk.Label(e_frame, wraplength=780, justify="left",
                  text="Ré-applique les 4 variables critiques (boot NVMe + régulateur "
                       "NPU). Inutile après un flash d'image COMPLÈTE (env inclus) ; "
                       "utile après un reflash de l'env d'origine.").pack(anchor="w", padx=4)
        erow = ttk.Frame(e_frame); erow.pack(fill="x")
        ttk.Button(erow, text="Appliquer les 4 vars d'env",
                   command=self._on_env_apply).pack(side="left", **btn)
        ttk.Button(erow, text="Sauver l'env (fw_printenv)",
                   command=self._on_env_dump).pack(side="left", **btn)

        # --- Activité + journal ---
        self.activity = ttk.Progressbar(self, mode="indeterminate")
        self.activity.pack(fill="x", padx=8, pady=(4, 0))
        log_frame = ttk.LabelFrame(self, text="Journal")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(log_frame, height=14, state="disabled")
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
                    self.log.configure(state="normal")
                    self.log.insert(tk.END, payload + "\n")
                    self.log.see(tk.END)
                    self.log.configure(state="disabled")
                elif kind == "done":
                    self._busy_stop()
                    if payload:
                        messagebox.showinfo("Terminé", payload)
                elif kind == "error":
                    self._busy_stop()
                    messagebox.showerror("Erreur", payload)
        except queue.Empty:
            pass
        self.after(100, self._pump_ui_queue)

    def log_write(self, text):
        self._ui_queue.put(("log", text))

    # ---------- État occupé ----------
    def _busy_start(self):
        self._busy = True
        for b in self._action_buttons:
            b.config(state="disabled")
        self.activity.start(12)

    def _busy_stop(self):
        self.activity.stop()
        for b in self._action_buttons:
            b.config(state="normal")
        self._busy = False

    def _start(self, target, *args):
        """Lance une action en thread (si pas déjà occupé)."""
        if self._busy:
            return
        self._busy_start()
        threading.Thread(target=self._guard(target), args=args, daemon=True).start()

    def _guard(self, target):
        def wrapped(*args):
            try:
                target(*args)
            except Exception as e:                       # filet : jamais de thread muet
                self.log_write(f"\n=== ERREUR ===\n{e}")
                self._ui_queue.put(("error", str(e)))
        return wrapped

    def _ops(self):
        """SpiOps frais, avec l'état simulation capturé au lancement de l'action."""
        return SpiOps(log=self.log_write, golden_dir=self._golden_dir,
                      sim=self.var_sim.get())

    def _programmer(self):
        return self.method_var.get()

    def _on_method_change(self):
        """Met à jour l'explication sous les boutons radio selon la méthode."""
        if self.method_var.get() == MTD_PROGRAMMER:
            self.method_info.config(text=(
                "À CHAUD, sur l'Odroid où tourne la station : lit / vérifie / "
                "reflashe sa PROPRE puce SPI en réassemblant ses partitions MTD "
                "(/dev/mtd*). Aucun démontage ni pince. C'est la voie on-device "
                "(flashrom -p internal n'existe pas sur cette carte)."))
        else:
            self.method_info.config(text=(
                "À la pince SOIC-8 sur la puce, carte HORS TENSION (flashrom "
                "ch341a_spi). Méthode de référence pour lire le master et "
                "flasher une carte vierge ou briquée."))

    # ---------- Golden : lecture / vérif ----------
    def _on_read_master(self):
        golden = self.golden_entry.get().strip()
        if not golden:
            messagebox.showerror("Erreur", "Renseigne le chemin du golden.")
            return
        self.log_write(f"\n--- Lecture de la puce ({self._programmer()}) ---")
        self._start(self._read_master_worker, self._ops(), self._programmer(), golden)

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
        self.log_write(f"\n--- Vérif puce vs golden ({self._programmer()}) ---")
        self._start(self._verify_chip_worker, self._ops(), self._programmer(), golden)

    def _verify_chip_worker(self, ops, programmer, golden):
        same = ops.verify_chip(programmer, golden)
        if same is None:
            self._ui_queue.put(("done", "Simulation : vérif non effectuée."))
        elif same:
            self._ui_queue.put(("done", "Puce IDENTIQUE au golden ✔"))
        else:
            self._ui_queue.put(("error", "La puce DIFFÈRE du golden (voir journal)."))

    # ---------- Flash d'une unité ----------
    def _on_flash_unit(self):
        programmer = self._programmer()
        golden = self.golden_entry.get().strip()
        if not self.var_sim.get():
            if not os.path.isfile(golden):
                messagebox.showerror("Erreur", f"Golden absent : {golden}")
                return
            live = " (à CHAUD, la puce de CETTE machine)" \
                if programmer == MTD_PROGRAMMER else ""
            if not messagebox.askyesno(
                    "Confirmation",
                    f"Ceci EFFACE la SPI de l'unité via « {human_programmer(programmer)} »"
                    f"{live} et y écrit le golden.\n\nUne sauvegarde de la puce "
                    "est faite avant.\n\nContinuer ?"):
                return
        self.log_write(f"\n--- Flash unité ({programmer}) ---")
        self._start(self._flash_unit_worker, self._ops(), programmer, golden)

    def _flash_unit_worker(self, ops, programmer, golden):
        backup = ops.flash_unit(programmer, golden)
        if backup is None:
            self._ui_queue.put(("done", "Simulation : flash non effectué."))
        else:
            self._ui_queue.put(("done", "Flash réussi et vérifié.\nSauvegarde "
                                        f"pré-flash : {backup}"))

    # ---------- Env U-Boot ----------
    def _on_env_apply(self):
        if not self.var_sim.get() and not messagebox.askyesno(
                "Confirmation", "Écrire les 4 variables d'env U-Boot (mtd1) via "
                "fw_setenv ?"):
            return
        self.log_write("\n--- Application des 4 vars d'env ---")
        self._start(self._env_apply_worker, self._ops())

    def _env_apply_worker(self, ops):
        ops.env_apply()
        self._ui_queue.put(("done", "Simulation : env non modifié." if ops.sim
                            else "4 variables d'env appliquées (mtd1)."))

    def _on_env_dump(self):
        self.log_write("\n--- Sauvegarde de l'env (fw_printenv) ---")
        self._start(self._env_dump_worker, self._ops())

    def _env_dump_worker(self, ops):
        path = ops.env_dump()
        self._ui_queue.put(("done", "Simulation : env non lu." if path is None
                            else f"Env sauvegardé : {path}"))
