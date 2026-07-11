#!/usr/bin/env python3
"""
GUI de clonage d'un disque Odroid -> clé USB / carte / NVMe (clone à FROID).

Usage : depuis un PC LINUX (ou WSL2 + usbipd), cloner soit un disque Odroid
branché en lecteur USB, soit un fichier image (.img), vers une clé/carte.

`ClonePanel` est un `ttk.Frame` embarquable (utilisé comme onglet dans le tableau
de bord unifié `station_gui.py`, qui regroupe SPI + Clone NVMe + Vérif) ; ce
fichier reste aussi lançable seul (`__main__` en bas) pour l'usage manuel/dev — un
`tk.Tk()` root qui monte juste ce panel. Le MOTEUR du clonage (montages, rsync,
identité, initramfs…) vit dans `clone_engine.CloneEngine`, partagé avec le CLI
headless `clone_cli.py` (SSH sans X11) : même code, mêmes garde-fous des deux
côtés.

Clone à FROID uniquement : la source ne doit JAMAIS être le disque qui porte le
système en cours d'exécution. L'outil refuse un disque système en source comme
en destination (voir `clone_engine.assert_not_system_disk`). Cloner un système
en marche a été retiré : copier une racine vivante donne un clone incohérent
(fichiers modifiés en cours de copie, droits/état système bancals) qui, au
mieux, boote mal — d'où la règle « disque à l'arrêt, lu en lecture seule ».
Pour dupliquer un Odroid, éteins-le, sors sa carte/eMMC et branche-la en
lecteur USB sur le PC.

MODE DE BOOT (important, choisi explicitement dans l'UI — jamais déduit du
contenu du disque) :
  - « SPI » (DÉFAUT, toute la flotte) : le bootloader (idbloader-spi + u-boot
    Armbian) vit dans la PUCE SPI de la carte, pas sur le disque. Le NVMe boote
    seul via `distro_bootcmd` d'u-boot en SPI (voir docs/DEPLOIEMENT_FLOTTE.md).
    Le clone n'a donc PAS besoin de zone bootloader sur le disque ; un idbloader
    résiduel (vestige d'un ancien clone) y est laissé sans effet. Le clone ne
    bootera que sur une carte dont la SPI a été flashée avec le golden
    (spi_flash_gui.py) ;
  - « Disque » (legacy eMMC/SD auto-bootable, hors flotte) : recopie la ZONE
    BOOTLOADER BRUTE avant la première partition (dd, secteurs 64 -> début p1 ;
    `idbloader.img` @64, `u-boot.itb` @16384, hors partition donc invisibles
    pour rsync) et vérifie le secteur 64.

Ce que fait le clone dans tous les cas (nécessaire pour que l'Odroid boote dessus) :
  - le TYPE de chaque système de fichiers (vfat/ext2/ext4), détecté sur la
    source — la BOOT des images ODROID est en vfat, pas en ext2 ;
  - une IDENTITÉ NEUVE : chaque système de fichiers reçoit un UUID frais
    (généré par mkfs) et le disque un nouveau label-id (donc de nouveaux
    PARTUUID). On NE recopie PAS l'identité de la source : si la source et le
    clone restent branchés ensemble avec des UUID identiques, le noyau/systemd
    peut monter la mauvaise partition. Les labels de volume, eux, sont conservés
    (cosmétiques, non utilisés pour désigner la racine sur ODROID) ;
  - la COHÉRENCE de la config : le fstab et la config du bootloader du clone
    (extlinux.conf/boot.ini/armbianEnv.txt, et `boot.scr` binaire dont les CRC
    sont recalculés) sont réécrits pour référencer sa NOUVELLE identité, sinon le
    noyau chercherait sa racine via l'UUID de la source et ne la trouverait pas ;
  - l'INITRAMFS est reconstruit (chroot + update-initramfs) pour embarquer les
    pilotes du disque cible (NVMe/PCIe, SATA...) : un initramfs hérité d'un
    système qui bootait ailleurs (SD/USB) ne verrait pas le NVMe au démarrage
    (« Gave up waiting for root device »). Ne marche que sur un hôte de MÊME
    architecture que le clone (ARM64 -> lancer sur l'ODROID) ; sinon l'étape est
    sautée avec un avertissement ;
  - la dernière partition est étendue pour remplir la destination.

Support de boot séparé (option, LEGACY) : depuis le passage au boot NVMe direct
via u-boot en SPI, un NVMe n'a PLUS besoin de support séparé — il boote seul.
Cette option ne reste utile que pour un SSD **USB-SATA** (pont UAS que le stack
USB d'u-boot ne pilote pas) : on prépare alors une clé USB/SD de boot distincte
(kernel + initrd + boot.scr → UUID de la cible), u-boot lit ce support, la racine
reste sur le SSD. Ne PAS la cocher pour un NVMe.

Nécessite : Linux, Python3 + tkinter, util-linux (sfdisk, blkid, findmnt,
blockdev, wipefs), parted, e2fsprogs (mkfs.ext*), dosfstools (mkfs.vfat), rsync,
et — pour l'initramfs — initramfs-tools dans le clone + un hôte ARM64.

Lancer avec : sudo python3 clone_odroid_gui.py
(besoin de root pour accéder aux disques bruts)
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from clone_core import list_block_devices
from clone_engine import CloneEngine, assert_not_system_disk


class ClonePanel(ttk.Frame):
    """Panel de clonage NVMe, embarquable dans n'importe quel conteneur tkinter
    (`master`) : `station_gui.py` le monte comme onglet, le wrapper standalone en
    bas de ce fichier le monte seul dans un `tk.Tk()`. Le travail est délégué à
    `clone_engine.CloneEngine` (partagé avec le CLI headless `clone_cli.py`) —
    ce panel ne fait que la sélection, la confirmation et l'affichage."""

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

        # --- Destination ---
        dst_frame = ttk.LabelFrame(self, text="Destination (disque cible — sera EFFACÉE)")
        dst_frame.pack(fill="x", **pad)

        self.dst_combo = ttk.Combobox(dst_frame, width=60, state="readonly")
        self.dst_combo.grid(row=0, column=0, columnspan=2, sticky="we", padx=4, pady=4)

        # Mode de boot EXPLICITE (jamais déduit du contenu du disque : un
        # idbloader résiduel sur le NVMe master ne le rend pas auto-bootable).
        self.boot_mode = tk.StringVar(value="spi")
        ttk.Label(dst_frame, text="Mode de boot de la cible :").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))
        ttk.Radiobutton(
            dst_frame, variable=self.boot_mode, value="spi",
            text="SPI (défaut, flotte) — u-boot en puce SPI, le NVMe boote seul",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=16)
        ttk.Radiobutton(
            dst_frame, variable=self.boot_mode, value="disk",
            text="Disque (legacy eMMC/SD auto-bootable) — recopie idbloader/u-boot",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=16)

        # Support de boot séparé (LEGACY) : uniquement un SSD USB-SATA (pont UAS
        # qu'u-boot ne pilote pas). Un NVMe boote seul via la SPI -> ne PAS cocher.
        self.var_bootmedium = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            dst_frame, variable=self.var_bootmedium, command=self._toggle_bootmedium,
            text="(Legacy) SSD USB-SATA seulement : préparer un support de boot "
                 "séparé (USB/SD, sera EFFACÉ). Inutile pour un NVMe.",
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))
        self.bootmedium_combo = ttk.Combobox(dst_frame, width=60, state="disabled")
        self.bootmedium_combo.grid(row=5, column=0, columnspan=2, sticky="we",
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

    def _browse_image(self):
        path = filedialog.askopenfilename(filetypes=[("Images disque", "*.img *.iso *.bin"), ("Tous", "*.*")])
        if path:
            self.src_image_entry.delete(0, tk.END)
            self.src_image_entry.insert(0, path)

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

    # ---------- Clonage ----------
    def _on_clone(self):
        dst_label = self.dst_combo.get()
        if not dst_label:
            messagebox.showerror("Erreur", "Sélectionne un disque de destination.")
            return
        dst_disk = self._disks[dst_label]

        img_path = None
        if self.src_mode.get() == "disk":
            src_label = self.src_disk_combo.get()
            if not src_label:
                messagebox.showerror("Erreur", "Sélectionne un disque source.")
                return
            src_disk = self._disks[src_label]
        else:
            img_path = self.src_image_entry.get().strip()
            if not img_path or not os.path.isfile(img_path):
                messagebox.showerror("Erreur", "Sélectionne un fichier image valide.")
                return
            src_disk = None  # sera résolu après losetup

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

        # GARDE-FOU : refuser le disque qui porte le système en cours d'exécution,
        # aussi bien en SOURCE (clone à froid uniquement : on ne clone jamais un
        # système vivant, cf. docstring) qu'en DESTINATION/support de boot (ne
        # jamais l'écraser).
        try:
            if src_disk is not None:
                assert_not_system_disk(src_disk, role="source")
            assert_not_system_disk(dst_disk, role="destination")
            if boot_medium is not None:
                assert_not_system_disk(boot_medium, role="support de boot")
        except RuntimeError as e:
            messagebox.showerror("Erreur", str(e))
            return

        efface = f"EFFACER TOUT LE CONTENU de {dst_disk}"
        if boot_medium:
            efface += f"\nET de {boot_medium} (support de boot)"
        if not messagebox.askyesno("Confirmation", f"Ceci va {efface}.\n\nContinuer ?"):
            return

        self._boot_medium = boot_medium
        # Capturé ici (thread principal) : le worker ne doit pas lire une Tk var.
        self._boot_mode_val = self.boot_mode.get()
        self.clone_btn.config(state="disabled")
        threading.Thread(target=self._clone_worker, args=(src_disk, dst_disk, img_path),
                         daemon=True).start()

    def _clone_worker(self, src_disk, dst_disk, img_path):
        engine = CloneEngine(log=self.log_write, progress=self._set_progress,
                             boot_mode=self._boot_mode_val,
                             boot_medium=self._boot_medium)
        try:
            msg = engine.clone(src_disk, dst_disk, img_path=img_path)
            self._ui_queue.put(("done", msg))
        except Exception as e:
            self.log_write(f"\n=== ERREUR ===\n{e}")
            self._ui_queue.put(("error", str(e)))


def _fix_x11_env_for_sudo():
    """
    Quand on lance avec 'sudo', root n'hérite pas forcément du cookie X11
    (~/.Xauthority) de l'utilisateur original -> Tkinter ne peut pas se
    connecter au display (erreur 'couldn't connect to display').
    On corrige ça automatiquement en repointant XAUTHORITY vers le
    .Xauthority de l'utilisateur qui a lancé sudo (SUDO_USER).

    Utile pour l'usage STANDALONE/dev (`sudo python3 clone_odroid_gui.py` depuis
    une session X déjà ouverte). Inutile sur le poste kiosque (station_gui.py) :
    là, root démarre X lui-même via systemd (pas de sudo dans la boucle).
    """
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user:
        return  # pas lancé via sudo, rien à faire

    try:
        import pwd
        home = pwd.getpwnam(sudo_user).pw_dir
    except Exception:
        return

    xauth_path = os.path.join(home, ".Xauthority")
    if os.path.isfile(xauth_path):
        os.environ["XAUTHORITY"] = xauth_path

    # Certains environnements (ex: sessions SSH avec X forwarding type
    # MobaXterm) ne propagent pas DISPLAY à root non plus.
    if "DISPLAY" not in os.environ:
        try:
            out = subprocess.run(
                ["bash", "-c", f"sudo -u {sudo_user} env | grep ^DISPLAY="],
                capture_output=True, text=True
            ).stdout.strip()
            if out.startswith("DISPLAY="):
                os.environ["DISPLAY"] = out.split("=", 1)[1]
        except Exception:
            pass
        os.environ.setdefault("DISPLAY", ":0")


if __name__ == "__main__":
    if os.name != "posix":
        print("Cet outil doit tourner sous Linux (sur l'Odroid, un PC Linux ou "
              "WSL2) : il utilise lsblk, sfdisk, mount et rsync.")
        sys.exit(1)

    if os.geteuid() != 0:
        print("Lance avec: sudo python3 clone_odroid_gui.py")
        sys.exit(1)

    # util-linux >= 2.39 passe par la nouvelle API de montage du noyau, qui
    # renvoie des « mount failed: Operation not permitted » injustifiés dans
    # certains environnements (WSL2, noyaux BSP...). On force l'appel mount(2)
    # classique : sans effet là où tout va bien, corrige le reste.
    os.environ.setdefault("LIBMOUNT_FORCE_MOUNT2", "always")

    _fix_x11_env_for_sudo()

    try:
        root = tk.Tk()
        root.title("Clone Odroid -> Clé USB")
        root.geometry("720x620")
        ClonePanel(root).pack(fill="both", expand=True)   # __init__ appelle déjà refresh_disks()
    except tk.TclError as e:
        print(f"Impossible d'ouvrir la fenêtre graphique: {e}")
        print("Essaie plutôt: sudo -E python3 clone_odroid_gui.py")
        print("(ou vérifie que ton client SSH a bien le X11 forwarding activé)")
        sys.exit(1)

    root.mainloop()
