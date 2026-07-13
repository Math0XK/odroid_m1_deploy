#!/usr/bin/env python3
"""
clone_panel.py — onglet Clone du tableau de bord (`station.py`) : clonage d'un
disque Odroid (ou d'une image) vers un disque cible, OU création d'une image
disque COMPACTE servant ensuite de source de clonage.

`ClonePanel` est un `ttk.Frame` embarquable, monté comme onglet par `station.py`
(entrée unique GUI + CLI). Le MOTEUR (montages, rsync, identité, initramfs…) vit
dans `clone_engine.CloneEngine`, partagé avec les sous-commandes CLI
`station.py clone` / `station.py image` (SSH sans X11) : même code, mêmes
garde-fous des deux côtés.

SOURCE — trois façons :
  - disque physique à FROID (défaut) : l'Odroid source éteint, sa carte/eMMC/NVMe
    branché en lecteur USB. La source est remontée read-only : instantané figé,
    le chemin recommandé pour un master de flotte ;
  - fichier image (.img) : monté en loop device (typiquement une image compacte
    produite ici même) ;
  - cette machine EN MARCHE (case « auto-clonage à chaud ») : autorise le disque
    système en source pour cloner/imager l'Odroid sur lequel la station tourne.
    Impossible de remonter « / » read-only -> lecture des montages vivants,
    rsync tolère les fichiers qui bougent (code 24). Un instantané à chaud n'est
    jamais parfaitement cohérent : arrêter les services applicatifs avant.

DESTINATION — deux façons :
  - disque physique (sera EFFACÉ) : le clonage classique ;
  - fichier image COMPACT (.img) : même pipeline, mais vers un fichier attaché
    en loop device et DIMENSIONNÉ sur l'espace UTILISÉ de la racine source (pas
    la capacité du disque — un NVMe 128 Go rempli à 20 % donne ~30 Go), sparse
    de surcroît. Au clonage depuis cette image, la racine est ré-étendue à la
    taille de la vraie cible.

MODE DE BOOT (explicite, jamais déduit du contenu du disque) :
  - « SPI » (DÉFAUT, toute la flotte) : le bootloader (idbloader-spi + u-boot
    Armbian) vit dans la PUCE SPI de la carte, pas sur le disque. Le clone ne
    bootera que sur une carte dont la SPI porte le golden (onglet SPI) ;
  - « Disque » (legacy eMMC/SD auto-bootable, hors flotte) : recopie la ZONE
    BOOTLOADER BRUTE (dd, secteurs 64 -> début p1) et vérifie le secteur 64.

Ce que fait le clone dans tous les cas : mêmes types de fs que la source,
IDENTITÉ NEUVE (UUID/PARTUUID frais), fstab + config bootloader + `boot.scr`
(CRC recalculés) réécrits vers cette identité, initramfs reconstruit (hôte ARM64
requis, sinon sauté avec avertissement), dernière partition étendue à la
destination. Voir le docstring de `clone_engine.py` pour le COMMENT.

Support de boot séparé (option, LEGACY) : uniquement pour un SSD USB-SATA (pont
UAS que le stack USB d'u-boot ne pilote pas). Ne PAS cocher pour un NVMe.
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from clone_core import list_block_devices
from clone_engine import CloneEngine, assert_not_system_disk


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

        self._build_ui()
        self.after(100, self._pump_ui_queue)
        self.refresh_disks()

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # --- Source ---
        src_frame = ttk.LabelFrame(self, text="Source")
        src_frame.pack(fill="x", **pad)

        self.src_mode = tk.StringVar(value="disk")
        ttk.Radiobutton(src_frame, text="Disque physique", variable=self.src_mode,
                        value="disk", command=self._toggle_src).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(src_frame, text="Fichier image (.img)", variable=self.src_mode,
                        value="image", command=self._toggle_src).grid(row=0, column=1, sticky="w")

        self.src_disk_combo = ttk.Combobox(src_frame, width=60, state="readonly")
        self.src_disk_combo.grid(row=1, column=0, columnspan=2, sticky="we", padx=4, pady=4)

        self.src_image_entry = ttk.Entry(src_frame, width=50)
        self.src_image_btn = ttk.Button(src_frame, text="Parcourir...", command=self._browse_image)
        # placés/masqués dynamiquement par _toggle_src

        # Auto-clonage à CHAUD : autorise le disque système (celui de la machine
        # où la station tourne) en source. Sinon, clone à froid uniquement.
        self.var_live = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            src_frame, variable=self.var_live,
            text="Auto-clonage à CHAUD : autoriser le disque SYSTÈME de cette "
                 "machine en source (arrêter les services qui écrivent ; "
                 "préférer le clone à froid pour un master)",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))

        # --- Destination ---
        dst_frame = ttk.LabelFrame(self, text="Destination")
        dst_frame.pack(fill="x", **pad)

        # Disque physique (clonage, destination EFFACÉE) ou fichier image
        # COMPACT (taillé sur l'espace UTILISÉ de la source, sparse — future
        # source de clonage via « Fichier image » côté Source).
        self.dst_mode = tk.StringVar(value="disk")
        ttk.Radiobutton(dst_frame, text="Disque physique (sera EFFACÉ)",
                        variable=self.dst_mode, value="disk",
                        command=self._toggle_dst).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(dst_frame, text="Fichier image compact (.img) — source de clonage",
                        variable=self.dst_mode, value="image",
                        command=self._toggle_dst).grid(row=0, column=1, sticky="w")

        self.dst_combo = ttk.Combobox(dst_frame, width=60, state="readonly")
        self.dst_combo.grid(row=1, column=0, columnspan=2, sticky="we", padx=4, pady=4)

        self.dst_image_entry = ttk.Entry(dst_frame, width=50)
        self.dst_image_btn = ttk.Button(dst_frame, text="Parcourir...",
                                        command=self._browse_dst_image)
        # placés/masqués dynamiquement par _toggle_dst

        # Mode de boot EXPLICITE (jamais déduit du contenu du disque : un
        # idbloader résiduel sur le NVMe master ne le rend pas auto-bootable).
        self.boot_mode = tk.StringVar(value="spi")
        ttk.Label(dst_frame, text="Mode de boot de la cible :").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))
        ttk.Radiobutton(
            dst_frame, variable=self.boot_mode, value="spi",
            text="SPI (défaut, flotte) — u-boot en puce SPI, le NVMe boote seul",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=16)
        ttk.Radiobutton(
            dst_frame, variable=self.boot_mode, value="disk",
            text="Disque (legacy eMMC/SD auto-bootable) — recopie idbloader/u-boot",
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=16)

        # Support de boot séparé (LEGACY) : uniquement un SSD USB-SATA (pont UAS
        # qu'u-boot ne pilote pas). Un NVMe boote seul via la SPI -> ne PAS cocher.
        self.var_bootmedium = tk.BooleanVar(value=False)
        self.bootmedium_check = ttk.Checkbutton(
            dst_frame, variable=self.var_bootmedium, command=self._toggle_bootmedium,
            text="(Legacy) SSD USB-SATA seulement : préparer un support de boot "
                 "séparé (USB/SD, sera EFFACÉ). Inutile pour un NVMe.")
        self.bootmedium_check.grid(row=5, column=0, columnspan=2, sticky="w",
                                   padx=4, pady=(6, 0))
        self.bootmedium_combo = ttk.Combobox(dst_frame, width=60, state="disabled")
        self.bootmedium_combo.grid(row=6, column=0, columnspan=2, sticky="we",
                                   padx=4, pady=4)

        ttk.Button(self, text="Rafraîchir la liste des disques",
                   command=self.refresh_disks).pack(**pad)

        # --- Bouton clonage ---
        self.clone_btn = ttk.Button(self, text="Lancer le clonage", command=self._on_clone)
        self.clone_btn.pack(**pad)

        # --- Progression ---
        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=8)
        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100.0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(prog_frame, text="", width=28, anchor="w")
        self.progress_label.pack(side="left", padx=6)

        # --- Log ---
        log_frame = ttk.LabelFrame(self, text="Journal")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(log_frame, height=18, state="disabled")
        self.log.pack(fill="both", expand=True)

        self._toggle_src()
        self._toggle_dst()

    def _toggle_bootmedium(self):
        self.bootmedium_combo.configure(
            state="readonly" if self.var_bootmedium.get() else "disabled")

    def _toggle_src(self):
        if self.src_mode.get() == "disk":
            self.src_image_entry.grid_forget()
            self.src_image_btn.grid_forget()
            self.src_disk_combo.grid(row=1, column=0, columnspan=2, sticky="we", padx=4, pady=4)
        else:
            self.src_disk_combo.grid_forget()
            self.src_image_entry.grid(row=1, column=0, sticky="we", padx=4, pady=4)
            self.src_image_btn.grid(row=1, column=1, sticky="w", padx=4, pady=4)

    def _toggle_dst(self):
        if self.dst_mode.get() == "disk":
            self.dst_image_entry.grid_forget()
            self.dst_image_btn.grid_forget()
            self.dst_combo.grid(row=1, column=0, columnspan=2, sticky="we",
                                padx=4, pady=4)
            self.bootmedium_check.configure(state="normal")
            self._toggle_bootmedium()
            self.clone_btn.configure(text="Lancer le clonage")
        else:
            # destination = fichier image : pas de support de boot séparé
            # (on n'écrit aucun disque), case décochée et grisée.
            self.dst_combo.grid_forget()
            self.dst_image_entry.grid(row=1, column=0, sticky="we", padx=4, pady=4)
            self.dst_image_btn.grid(row=1, column=1, sticky="w", padx=4, pady=4)
            self.var_bootmedium.set(False)
            self.bootmedium_check.configure(state="disabled")
            self.bootmedium_combo.configure(state="disabled")
            self.clone_btn.configure(text="Créer l'image compacte")

    def _browse_image(self):
        path = filedialog.askopenfilename(filetypes=[("Images disque", "*.img *.iso *.bin"), ("Tous", "*.*")])
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
                    self.log.configure(state="normal")
                    self.log.insert(tk.END, payload + "\n")
                    self.log.see(tk.END)
                    self.log.configure(state="disabled")
                elif kind == "progress":
                    pct, text = payload
                    self.progress["value"] = pct
                    self.progress_label.config(text=text)
                elif kind == "done":
                    self.clone_btn.config(state="normal")
                    self.progress["value"] = 100.0
                    self.progress_label.config(text="Terminé ✔")
                    messagebox.showinfo("Terminé", payload)
                elif kind == "error":
                    self.clone_btn.config(state="normal")
                    self.progress_label.config(text="Erreur ✖")
                    messagebox.showerror("Erreur", payload)
        except queue.Empty:
            pass
        self.after(100, self._pump_ui_queue)

    def log_write(self, text):
        self._ui_queue.put(("log", text))

    def _set_progress(self, pct, text):
        self._ui_queue.put(("progress", (pct, text)))

    # ---------- Data ----------
    def refresh_disks(self):
        try:
            disks = list_block_devices()
        except Exception as e:
            messagebox.showerror("Erreur", str(e))
            return
        labels = []
        self._disks = {}
        for d in disks:
            label = f"{d['path']}  ({d['size']}, {d['model']})"
            if d["system"]:
                label += "  [disque système]"
            labels.append(label)
            self._disks[label] = d["path"]
        self.src_disk_combo["values"] = labels
        self.dst_combo["values"] = labels
        self.bootmedium_combo["values"] = labels
        self.log_write(f"{len(disks)} disque(s) détecté(s).")

    # ---------- Sélections communes ----------
    def _selected_source(self):
        """(src_disk, img_path) selon le mode source, ou None si invalide
        (l'erreur est déjà affichée)."""
        if self.src_mode.get() == "disk":
            src_label = self.src_disk_combo.get()
            if not src_label:
                messagebox.showerror("Erreur", "Sélectionne un disque source.")
                return None
            return self._disks[src_label], None
        img_path = self.src_image_entry.get().strip()
        if not img_path or not os.path.isfile(img_path):
            messagebox.showerror("Erreur", "Sélectionne un fichier image valide.")
            return None
        return None, img_path   # src résolu après losetup

    def _check_source_disk(self, src_disk, live):
        """Garde-fou disque système : sauté UNIQUEMENT en auto-clonage à chaud
        explicite (case cochée). Retourne False si refusé (erreur affichée)."""
        if src_disk is None or live:
            return True
        try:
            assert_not_system_disk(src_disk, role="source")
        except RuntimeError as e:
            messagebox.showerror(
                "Erreur", f"{e}\n\nPour cloner CETTE machine en marche, coche "
                "« Auto-clonage à CHAUD » (source).")
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
        live = self.var_live.get() and src_disk is not None

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

        # GARDE-FOUS : le disque système n'est jamais ÉCRASÉ (destination /
        # support de boot, absolu) ; en source il n'est accepté qu'en
        # auto-clonage à chaud explicite.
        if not self._check_source_disk(src_disk, live):
            return
        try:
            assert_not_system_disk(dst_disk, role="destination")
            if boot_medium is not None:
                assert_not_system_disk(boot_medium, role="support de boot")
        except RuntimeError as e:
            messagebox.showerror("Erreur", str(e))
            return

        efface = f"EFFACER TOUT LE CONTENU de {dst_disk}"
        if boot_medium:
            efface += f"\nET de {boot_medium} (support de boot)"
        if live:
            efface += ("\n\nSource = SYSTÈME EN MARCHE (clone à chaud) : "
                       "arrête d'abord les services qui écrivent.")
        if not messagebox.askyesno("Confirmation", f"Ceci va {efface}.\n\nContinuer ?"):
            return

        self._boot_medium = boot_medium
        # Capturés ici (thread principal) : le worker ne doit pas lire une Tk var.
        self._boot_mode_val = self.boot_mode.get()
        self.clone_btn.config(state="disabled")
        threading.Thread(target=self._clone_worker,
                         args=(src_disk, dst_disk, img_path, live),
                         daemon=True).start()

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
        live = self.var_live.get()

        if not self._check_source_disk(src_disk, live):
            return

        msg = (f"Créer une image COMPACTE de {src_disk} dans :\n{out_path}\n\n"
               "(taillée sur l'espace utilisé, fichier sparse)")
        if os.path.exists(out_path):
            msg += "\n\n⚠ Le fichier existe déjà et sera REMPLACÉ."
        if live:
            msg += ("\n\nSource = SYSTÈME EN MARCHE (imagerie à chaud) : "
                    "arrête d'abord les services qui écrivent.")
        if not messagebox.askyesno("Confirmation", msg + "\n\nContinuer ?"):
            return

        self._boot_mode_val = self.boot_mode.get()
        self.clone_btn.config(state="disabled")
        threading.Thread(target=self._image_worker, args=(src_disk, out_path, live),
                         daemon=True).start()

    # ---------- Workers ----------
    def _clone_worker(self, src_disk, dst_disk, img_path, live):
        engine = CloneEngine(log=self.log_write, progress=self._set_progress,
                             boot_mode=self._boot_mode_val,
                             boot_medium=self._boot_medium, live=live)
        try:
            msg = engine.clone(src_disk, dst_disk, img_path=img_path)
            self._ui_queue.put(("done", msg))
        except Exception as e:
            self.log_write(f"\n=== ERREUR ===\n{e}")
            self._ui_queue.put(("error", str(e)))

    def _image_worker(self, src_disk, out_path, live):
        engine = CloneEngine(log=self.log_write, progress=self._set_progress,
                             boot_mode=self._boot_mode_val, live=live)
        try:
            msg = engine.make_image(src_disk, out_path)
            self._ui_queue.put(("done", msg))
        except Exception as e:
            self.log_write(f"\n=== ERREUR ===\n{e}")
            self._ui_queue.put(("error", str(e)))
