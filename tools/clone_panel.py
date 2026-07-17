#!/usr/bin/env python3
"""
clone_panel.py — onglet Clone du tableau de bord (`station.py`) : clonage d'un
disque Odroid (ou d'une image) vers un disque cible, OU création d'une image
disque COMPACTE servant ensuite de source de clonage.

`ClonePanel` est un `ttk.Frame` embarquable, monté comme onglet par `station.py`
(entrée unique GUI + CLI). Le MOTEUR (montages, rsync, identité, initramfs,
audit de boot…) vit dans `clone_engine.CloneEngine`, partagé avec les
sous-commandes CLI `station.py clone` / `station.py image` (SSH sans X11) :
même code, mêmes garde-fous des deux côtés.

INTERFACE (refonte) : trois sections numérotées (Source, Destination, Lancer),
un BANDEAU D'ÉTAT toujours visible (préparation / étape en cours / GO / échec),
une barre de progression avec l'étape courante et le temps écoulé, et un
JOURNAL EN COULEURS alimenté par les niveaux du `report.Reporter` (étapes en
gras, succès en vert, avertissements en orange, erreurs en rouge). Le disque
système n'apparaît JAMAIS dans la liste des destinations.

SOURCE — trois façons (À FROID uniquement : le disque système en marche est
toujours refusé en source) : disque physique branché en USB (Odroid source
éteint), fichier image .img (loop device), ou sauvegarde partclone (dossier
.sfdisk + .pc). DESTINATION — disque physique (EFFACÉ) ou fichier image
compact. MODE DE BOOT explicite : « SPI » (défaut flotte) ou « disque »
(legacy). Voir les docstrings de `clone_engine.py` pour le POURQUOI des étapes.
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from clone_core import (disk_display_label, find_partclone_bundle,
                        list_block_devices)
from clone_engine import CloneEngine, assert_not_system_disk
from report import Reporter
from ui_widgets import LogView, ScrollableFrame, StatusBanner, hint, scroll_height, section


class ClonePanel(ttk.Frame):
    """Panel de clonage/imagerie, embarquable dans n'importe quel conteneur
    tkinter (`master`) : `station.py` le monte comme onglet. Le travail est
    délégué à `clone_engine.CloneEngine` (partagé avec le CLI) — ce panel ne
    fait que la sélection, la confirmation et l'affichage."""

    def __init__(self, master):
        super().__init__(master)

        self._boot_medium = None      # disque de boot séparé (option), sinon None
        self._boot_mode_val = "spi"   # mode de boot capturé au lancement du clone
        self._ui_queue = queue.Queue()  # thread de travail -> UI (Tk n'est pas thread-safe)
        self._busy_since = None

        self._build_ui()
        self.after(100, self._pump_ui_queue)
        self.refresh_disks()

    # ---------- UI ----------
    def _build_ui(self):
        # Contrôles de config (1/2/3) : zone défilante de hauteur fixe, pour que
        # le bandeau/la progression/le journal packés après restent TOUJOURS
        # visibles, même sur un écran bas (le nombre de contrôles ici dépasse
        # facilement 720p à lui seul).
        scroll = ScrollableFrame(self, height=scroll_height(self))
        scroll.pack(side="top", fill="x")
        body = scroll.body

        # --- 1 · Source ---
        src_frame = section(body, "1 · Source (lue seulement, jamais modifiée)")
        self.src_mode = tk.StringVar(value="disk")
        radios = ttk.Frame(src_frame)
        radios.pack(fill="x", padx=4)
        for text, val in (("Disque physique", "disk"),
                          ("Fichier image (.img)", "image"),
                          ("Sauvegarde partclone (dossier)", "bundle")):
            ttk.Radiobutton(radios, text=text, variable=self.src_mode,
                            value=val, command=self._toggle_src).pack(
                side="left", padx=(0, 18))

        self.src_row = ttk.Frame(src_frame)
        self.src_row.pack(fill="x", padx=4, pady=4)
        self.src_disk_combo = ttk.Combobox(self.src_row, state="readonly")
        self.src_image_entry = ttk.Entry(self.src_row)
        self.src_image_btn = ttk.Button(self.src_row, text="Parcourir…",
                                        command=self._browse_image)
        hint(src_frame, "Clone à FROID uniquement : éteins l'Odroid source et "
                        "branche sa carte/eMMC/NVMe en lecteur USB. Le disque "
                        "système de CE poste est refusé automatiquement.")

        # --- 2 · Destination ---
        dst_frame = section(body, "2 · Destination")
        self.dst_mode = tk.StringVar(value="disk")
        dradios = ttk.Frame(dst_frame)
        dradios.pack(fill="x", padx=4)
        ttk.Radiobutton(dradios, text="Disque physique — sera EFFACÉ",
                        variable=self.dst_mode, value="disk",
                        command=self._toggle_dst).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(dradios, text="Fichier image compact (.img) — future source de clonage",
                        variable=self.dst_mode, value="image",
                        command=self._toggle_dst).pack(side="left")

        self.dst_row = ttk.Frame(dst_frame)
        self.dst_row.pack(fill="x", padx=4, pady=4)
        self.dst_combo = ttk.Combobox(self.dst_row, state="readonly")
        self.dst_image_entry = ttk.Entry(self.dst_row)
        self.dst_image_btn = ttk.Button(self.dst_row, text="Parcourir…",
                                        command=self._browse_dst_image)

        # Mode de boot EXPLICITE (jamais déduit du contenu du disque : un
        # idbloader résiduel sur le NVMe master ne le rend pas auto-bootable).
        self.boot_mode = tk.StringVar(value="spi")
        ttk.Radiobutton(
            dst_frame, variable=self.boot_mode, value="spi",
            text="Boot SPI (défaut, toute la flotte) — u-boot vit dans la puce "
                 "SPI, le NVMe boote seul",
        ).pack(anchor="w", padx=16)
        ttk.Radiobutton(
            dst_frame, variable=self.boot_mode, value="disk",
            text="Boot disque (legacy eMMC/SD auto-bootable) — recopie "
                 "idbloader/u-boot sur le disque",
        ).pack(anchor="w", padx=16)

        # Support de boot séparé (LEGACY) : uniquement un SSD USB-SATA (pont UAS
        # qu'u-boot ne pilote pas). Un NVMe boote seul via la SPI -> ne PAS cocher.
        self.var_bootmedium = tk.BooleanVar(value=False)
        self.bootmedium_check = ttk.Checkbutton(
            dst_frame, variable=self.var_bootmedium, command=self._toggle_bootmedium,
            text="(Legacy) SSD USB-SATA seulement : préparer un support de boot "
                 "séparé (USB/SD, sera EFFACÉ). Inutile pour un NVMe.")
        self.bootmedium_check.pack(anchor="w", padx=4, pady=(6, 0))
        self.bootmedium_combo = ttk.Combobox(dst_frame, state="disabled")
        self.bootmedium_combo.pack(fill="x", padx=4, pady=(2, 6))

        # --- 3 · Lancement ---
        act = section(body, "3 · Lancer")
        arow = ttk.Frame(act)
        arow.pack(fill="x", padx=4, pady=4)
        ttk.Button(arow, text="↻  Rafraîchir les disques",
                   command=self.refresh_disks).pack(side="left")
        self.clone_btn = ttk.Button(arow, text="▶  LANCER LE CLONAGE",
                                    command=self._on_clone)
        self.clone_btn.pack(side="right")

        # --- Bandeau d'état + progression ---
        self.banner = StatusBanner(self)
        self.banner.pack(fill="x", padx=10, pady=(4, 0))

        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=10, pady=(4, 0))
        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100.0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(prog_frame, text="", width=34, anchor="w")
        self.progress_label.pack(side="left", padx=8)

        # --- Journal ---
        log_frame = ttk.LabelFrame(self, text=" Journal détaillé ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log = LogView(log_frame, height=16)
        self.log.pack(fill="both", expand=True)

        self._toggle_src()
        self._toggle_dst()

    def _toggle_bootmedium(self):
        self.bootmedium_combo.configure(
            state="readonly" if self.var_bootmedium.get() else "disabled")

    def _toggle_src(self):
        for w in (self.src_disk_combo, self.src_image_entry, self.src_image_btn):
            w.pack_forget()
        if self.src_mode.get() == "disk":
            self.src_disk_combo.pack(fill="x", expand=True)
        else:
            self.src_image_entry.pack(side="left", fill="x", expand=True)
            self.src_image_btn.pack(side="left", padx=(6, 0))

    def _toggle_dst(self):
        for w in (self.dst_combo, self.dst_image_entry, self.dst_image_btn):
            w.pack_forget()
        if self.dst_mode.get() == "disk":
            self.dst_combo.pack(fill="x", expand=True)
            self.bootmedium_check.configure(state="normal")
            self._toggle_bootmedium()
            self.clone_btn.configure(text="▶  LANCER LE CLONAGE")
        else:
            # destination = fichier image : pas de support de boot séparé
            # (on n'écrit aucun disque), case décochée et grisée.
            self.dst_image_entry.pack(side="left", fill="x", expand=True)
            self.dst_image_btn.pack(side="left", padx=(6, 0))
            self.var_bootmedium.set(False)
            self.bootmedium_check.configure(state="disabled")
            self.bootmedium_combo.configure(state="disabled")
            self.clone_btn.configure(text="▶  CRÉER L'IMAGE COMPACTE")

    def _browse_image(self):
        if self.src_mode.get() == "bundle":
            path = filedialog.askdirectory(title="Dossier de la sauvegarde partclone")
        else:
            path = filedialog.askopenfilename(
                filetypes=[("Images disque", "*.img *.iso *.bin"), ("Tous", "*.*")])
        if path:
            self.src_image_entry.delete(0, tk.END)
            self.src_image_entry.insert(0, path)

    def _browse_dst_image(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".img",
            filetypes=[("Image disque", "*.img"), ("Tous", "*.*")])
        if path:
            self.dst_image_entry.delete(0, tk.END)
            self.dst_image_entry.insert(0, path)

    # ---------- Passerelle thread de travail -> UI ----------
    # Tkinter n'est PAS thread-safe : le thread de clonage ne touche jamais un
    # widget directement, il poste dans une queue vidée ici (thread principal).
    def _pump_ui_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    level, text = payload
                    self.log.write(level, text)
                    if level == "step":
                        self.banner.set_state("busy", text)
                elif kind == "progress":
                    pct, text = payload
                    self.progress["value"] = pct
                    self.progress_label.config(text=self._with_elapsed(text))
                elif kind == "done":
                    self._busy_since = None
                    self.clone_btn.config(state="normal")
                    self.progress["value"] = 100.0
                    self.progress_label.config(text="Terminé ✔")
                    self.banner.set_state("ok", "TERMINÉ — audit de boot : GO ✔")
                    messagebox.showinfo("Terminé — GO", payload)
                elif kind == "error":
                    self._busy_since = None
                    self.clone_btn.config(state="normal")
                    self.progress_label.config(text="Échec ✖")
                    self.banner.set_state("error", "ÉCHEC — voir le journal "
                                                   "(le disque n'est PAS déployable)")
                    messagebox.showerror("Échec", payload)
        except queue.Empty:
            pass
        self.after(100, self._pump_ui_queue)

    def _with_elapsed(self, text):
        if self._busy_since is None:
            return text
        mins, secs = divmod(int(time.monotonic() - self._busy_since), 60)
        return f"{text}   ({mins:d} min {secs:02d} s)"

    def _make_reporter(self):
        """Reporter dont le sink/progress postent dans la queue UI (les
        moteurs tournent dans un thread de travail)."""
        return Reporter(
            sink=lambda level, text: self._ui_queue.put(("log", (level, text))),
            progress=lambda pct, text: self._ui_queue.put(("progress", (pct, text))))

    # ---------- Data ----------
    def refresh_disks(self):
        try:
            disks = list_block_devices()
        except Exception as e:
            messagebox.showerror("Erreur", str(e))
            return
        self._disks = {}
        src_labels, dst_labels = [], []
        for d in disks:
            label = disk_display_label(d)
            self._disks[label] = d["path"]
            src_labels.append(label)
            if not d["system"]:
                # le disque système n'est JAMAIS proposé en destination : il
                # serait refusé de toute façon (assert_not_system_disk), autant
                # ne pas le rendre sélectionnable.
                dst_labels.append(label)
        self.src_disk_combo["values"] = src_labels
        self.dst_combo["values"] = dst_labels
        self.bootmedium_combo["values"] = dst_labels
        self.log.write("info", f"{len(disks)} disque(s) détecté(s) — "
                               f"{len(dst_labels)} éligible(s) en destination.")

    # ---------- Sélections communes ----------
    def _selected_source(self):
        """(src_disk, img_path) selon le mode source, ou None si invalide
        (l'erreur est déjà affichée). En mode bundle, img_path est le DOSSIER de
        la sauvegarde partclone (le worker route vers restore_bundle)."""
        if self.src_mode.get() == "disk":
            src_label = self.src_disk_combo.get()
            if not src_label:
                messagebox.showerror("Erreur", "Sélectionne un disque source.")
                return None
            return self._disks[src_label], None
        img_path = self.src_image_entry.get().strip()
        if self.src_mode.get() == "bundle":
            if not img_path or find_partclone_bundle(img_path) is None:
                messagebox.showerror(
                    "Erreur", "Sélectionne un dossier de sauvegarde partclone "
                    "(table .sfdisk + images .pc).")
                return None
            return None, img_path
        if not img_path or not os.path.isfile(img_path):
            messagebox.showerror("Erreur", "Sélectionne un fichier image valide.")
            return None
        return None, img_path   # src résolu après losetup

    def _check_source_disk(self, src_disk):
        """Garde-fou disque système en source (clone à froid uniquement).
        Retourne False si refusé (erreur affichée)."""
        if src_disk is None:
            return True
        try:
            assert_not_system_disk(src_disk, role="source")
        except RuntimeError as e:
            messagebox.showerror("Erreur", str(e))
            return False
        return True

    # ---------- Lancement ----------
    def _on_clone(self):
        if self.dst_mode.get() == "image":
            self._on_make_image()
            return

        dst_label = self.dst_combo.get()
        if not dst_label:
            messagebox.showerror("Erreur", "Sélectionne un disque de destination.")
            return
        dst_disk = self._disks[dst_label]

        sel = self._selected_source()
        if sel is None:
            return
        src_disk, img_path = sel

        if src_disk is not None and src_disk == dst_disk:
            messagebox.showerror("Erreur", "Source et destination identiques.")
            return

        # Support de boot séparé (optionnel).
        boot_medium = None
        if self.var_bootmedium.get():
            bl = self.bootmedium_combo.get()
            if not bl:
                messagebox.showerror("Erreur", "Sélectionne le support de boot (USB/SD).")
                return
            boot_medium = self._disks[bl]
            if boot_medium in (src_disk, dst_disk):
                messagebox.showerror(
                    "Erreur", "Le support de boot doit être un disque DISTINCT de "
                    "la source et de la cible.")
                return

        # GARDE-FOUS : le disque système n'est jamais la source (clone à froid)
        # ni ÉCRASÉ (destination / support de boot), garde-fou absolu.
        if not self._check_source_disk(src_disk):
            return
        try:
            assert_not_system_disk(dst_disk, role="destination")
            if boot_medium is not None:
                assert_not_system_disk(boot_medium, role="support de boot")
        except RuntimeError as e:
            messagebox.showerror("Erreur", str(e))
            return

        efface = f"EFFACER TOUT LE CONTENU de :\n\n    {dst_label}"
        if boot_medium:
            efface += f"\n    {boot_medium} (support de boot)"
        if not messagebox.askyesno("Confirmation",
                                   f"Ceci va {efface}\n\nContinuer ?"):
            return

        self._boot_medium = boot_medium
        # Capturés ici (thread principal) : le worker ne doit pas lire une Tk var.
        self._boot_mode_val = self.boot_mode.get()
        self._start_worker(self._clone_worker, src_disk, dst_disk, img_path)

    def _on_make_image(self):
        if self.src_mode.get() != "disk":
            messagebox.showerror(
                "Erreur", "La création d'image se fait depuis un DISQUE physique "
                "(une image existe déjà sous forme de fichier).")
            return
        sel = self._selected_source()
        if sel is None:
            return
        src_disk, _ = sel
        out_path = self.dst_image_entry.get().strip()
        if not out_path:
            messagebox.showerror("Erreur", "Renseigne le fichier image à créer.")
            return
        parent = os.path.dirname(os.path.abspath(out_path))
        if not os.path.isdir(parent):
            messagebox.showerror("Erreur", f"Dossier inexistant : {parent}")
            return

        if not self._check_source_disk(src_disk):
            return

        msg = (f"Créer une image COMPACTE de {src_disk} dans :\n{out_path}\n\n"
               "(taillée sur l'espace utilisé, fichier sparse)")
        if os.path.exists(out_path):
            msg += "\n\n⚠ Le fichier existe déjà et sera REMPLACÉ."
        if not messagebox.askyesno("Confirmation", msg + "\n\nContinuer ?"):
            return

        self._boot_mode_val = self.boot_mode.get()
        self._start_worker(self._image_worker, src_disk, out_path)

    def _start_worker(self, target, *args):
        self.log.clear()
        self.banner.set_state("busy", "Démarrage…")
        self.progress["value"] = 0
        self._busy_since = time.monotonic()
        self.clone_btn.config(state="disabled")
        threading.Thread(target=target, args=args, daemon=True).start()

    # ---------- Workers ----------
    def _clone_worker(self, src_disk, dst_disk, img_path):
        engine = CloneEngine(self._make_reporter(),
                             boot_mode=self._boot_mode_val,
                             boot_medium=self._boot_medium)
        try:
            # img_path pointant sur un DOSSIER = sauvegarde partclone -> restore.
            if img_path and os.path.isdir(img_path):
                msg = engine.restore_bundle(find_partclone_bundle(img_path), dst_disk)
            else:
                msg = engine.clone(src_disk, dst_disk, img_path=img_path)
            self._ui_queue.put(("done", msg))
        except Exception as e:
            self._ui_queue.put(("log", ("error", f"ARRÊT SUR ERREUR :\n{e}")))
            self._ui_queue.put(("error", str(e)))

    def _image_worker(self, src_disk, out_path):
        engine = CloneEngine(self._make_reporter(), boot_mode=self._boot_mode_val)
        try:
            msg = engine.make_image(src_disk, out_path)
            self._ui_queue.put(("done", msg))
        except Exception as e:
            self._ui_queue.put(("log", ("error", f"ARRÊT SUR ERREUR :\n{e}")))
            self._ui_queue.put(("error", str(e)))
