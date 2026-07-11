#!/usr/bin/env python3
"""
GUI de gestion de la puce SPI Odroid-M1 : sauvegarde du « golden », flash de la
flotte, variables d'env U-Boot.

Contexte : sur ce projet le RK3568 boote son bootloader (idbloader-spi + u-boot
Armbian + env) **depuis la puce SPI 16 MiO**, et le NVMe boote seul derrière (voir
../docs/DEPLOIEMENT_FLOTTE.md). Toute cette config vit aujourd'hui sur un unique
master physique -> cet outil la sauvegarde durablement (sous `../images/spi/`,
versionné) et la reflashe sur les autres cartes.

`SpiPanel` est un `ttk.Frame` embarquable (utilisé comme onglet dans le tableau de
bord unifié `station_gui.py`, qui regroupe SPI + Clone NVMe + Vérif) ; ce fichier
reste aussi lançable seul (`__main__` en bas) pour l'usage manuel/dev — un
`tk.Tk()` root qui monte juste ce panel. La section « Vérif déploiement » vit à
part dans `verify_panel.py` (pertinente après un flash SPI COMME après un
clonage, donc pas rattachée à un seul des deux).

Deux façons de flasher, via le sélecteur « Programmer » (flashrom fait l'abstraction) :
  - `ch341a_spi` : à la PINCE (clip SOIC), carte hors tension — marche même sur une
    unité vierge/briquée. C'est la méthode de lecture du master.
  - `internal` / `linux_mtd:dev=0` : ON-DEVICE, en SSH sur une unité qui boote déjà
    (voir les réserves dans le runbook : le flash pleine-puce on-device dépend du
    support flashrom du contrôleur SPI RK3568).

Case « Mode simulation » : n'exécute RIEN, journalise seulement les commandes
flashrom/fw_setenv exactes (revue de sécurité, sans matériel).

Nécessite : Linux, Python3 + tkinter, flashrom (pince/on-device), u-boot-tools
(`fw_setenv`/`fw_printenv`) pour la section Env. Lancer avec les droits root pour
flasher : `sudo python3 spi_flash_gui.py`.
"""

import os
import queue
import shlex
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import spi_core as sc

# Programmers proposés : libellé lisible -> chaîne passée à flashrom `-p`.
PROGRAMMERS = {
    "Pince CH341A (carte hors tension)": "ch341a_spi",
    "On-device — internal (SSH)": "internal",
    "On-device — linux_mtd:dev=0 (1 MTD, avancé)": "linux_mtd:dev=0",
}


class SpiPanel(ttk.Frame):
    """Panel Golden / Flash flotte / Env U-Boot — embarquable dans n'importe quel
    conteneur tkinter (`master`) : `station_gui.py` le monte comme onglet, le
    wrapper standalone en bas de ce fichier le monte seul dans un `tk.Tk()`."""

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

        # --- Programmer (partagé lecture/flash/vérif puce) ---
        prog_frame = ttk.LabelFrame(self, text="Programmer (flashrom)")
        prog_frame.pack(fill="x", **pad)
        self.prog_combo = ttk.Combobox(prog_frame, width=48, state="readonly",
                                       values=list(PROGRAMMERS))
        self.prog_combo.current(0)   # pince par défaut (méthode actée)
        self.prog_combo.pack(side="left", padx=6, pady=6)
        self.var_sim = tk.BooleanVar(value=False)
        ttk.Checkbutton(prog_frame, variable=self.var_sim,
                        text="Mode simulation (n'exécute rien)").pack(side="left", padx=12)

        # --- Golden ---
        g_frame = ttk.LabelFrame(self, text="Golden (master de référence, 16 MiO)")
        g_frame.pack(fill="x", **pad)
        row = ttk.Frame(g_frame); row.pack(fill="x")
        ttk.Label(row, text="Fichier :").pack(side="left", padx=4)
        self.golden_entry = ttk.Entry(row, width=58)
        self.golden_entry.insert(0, default_golden)
        self.golden_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="Parcourir…", command=self._browse_golden).pack(side="left", padx=4)
        brow = ttk.Frame(g_frame); brow.pack(fill="x")
        ttk.Button(brow, text="Lire le master → golden",
                   command=self._on_read_master).pack(side="left", **btn)
        ttk.Button(brow, text="Vérifier la puce vs golden",
                   command=self._on_verify_chip).pack(side="left", **btn)

        # --- Flash unité ---
        f_frame = ttk.LabelFrame(self, text="Flasher une unité (EFFACE la SPI)")
        f_frame.pack(fill="x", **pad)
        ttk.Label(f_frame, wraplength=780, justify="left",
                  text="Sauvegarde la puce cible AVANT le flash, contrôle le golden "
                       "(taille + signature + SHA256), puis écrit et vérifie.").pack(
            anchor="w", padx=4)
        ttk.Button(f_frame, text="Flasher cette unité avec le golden",
                   command=self._on_flash_unit).pack(side="left", **btn)

        # --- Env U-Boot ---
        e_frame = ttk.LabelFrame(self, text="Variables d'env U-Boot (on-device, root)")
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

    # ---------- Exécution de commandes ----------
    def _run_cmd(self, argv, sim, allow_fail=False):
        """Exécute (ou simule) une commande. Retourne (returncode, sortie).

        En simulation : journalise la commande exacte, ne l'exécute pas, renvoie
        (0, ''). Sinon capture stdout+stderr et journalise la fin de sortie.
        """
        pretty = " ".join(shlex.quote(a) for a in argv)
        if sim:
            self.log_write(f"[SIMULATION] {pretty}")
            return 0, ""
        self.log_write(f"$ {pretty}")
        try:
            r = subprocess.run(argv, capture_output=True, text=True)
        except FileNotFoundError:
            raise RuntimeError(f"Commande introuvable : {argv[0]} "
                               "(paquet manquant ? flashrom / u-boot-tools)")
        out = (r.stdout or "") + (r.stderr or "")
        tail = "\n".join(out.splitlines()[-12:])
        if tail:
            self.log_write(tail)
        if r.returncode != 0 and not allow_fail:
            raise RuntimeError(f"Échec (code {r.returncode}) : {pretty}")
        return r.returncode, out

    def _programmer(self):
        return PROGRAMMERS[self.prog_combo.get()]

    def _sha_sidecar(self, golden):
        return golden + ".sha256"

    # ---------- Golden : lecture / vérif ----------
    def _on_read_master(self):
        programmer = self._programmer()
        golden = self.golden_entry.get().strip()
        sim = self.var_sim.get()
        if not golden:
            messagebox.showerror("Erreur", "Renseigne le chemin du golden.")
            return
        self.log_write(f"\n--- Lecture du master ({programmer}) ---")
        self._start(self._read_master_worker, programmer, golden, sim)

    def _read_master_worker(self, programmer, golden, sim):
        os.makedirs(os.path.dirname(golden) or ".", exist_ok=True)
        tmp = golden + ".read_tmp"
        self._run_cmd(sc.flashrom_cmd("read", programmer, tmp), sim)
        if sim:
            self._ui_queue.put(("done", "Simulation : lecture non effectuée."))
            return
        with open(tmp, "rb") as f:
            data = f.read()
        ok, reason = sc.looks_like_bootloader(data)
        if not ok:
            os.remove(tmp)
            raise RuntimeError(f"Dump refusé : {reason}. Golden NON écrit.")
        digest = sc.sha256_bytes(data)
        os.replace(tmp, golden)
        with open(self._sha_sidecar(golden), "w", encoding="utf-8") as f:
            f.write(f"{digest}  {os.path.basename(golden)}\n")
        if b"ethaddr=" in data:
            self.log_write("⚠ Le golden contient une MAC figée (ethaddr) : flasher "
                           "l'image complète donnerait la MÊME MAC à toute la flotte. "
                           "Voir DEPLOIEMENT_FLOTTE.md avant de flasher.")
        self.log_write(f"Golden écrit : {golden}\nSHA256 : {digest}")
        self._ui_queue.put(("done", f"Golden sauvegardé et vérifié.\n{golden}"))

    def _on_verify_chip(self):
        programmer = self._programmer()
        golden = self.golden_entry.get().strip()
        sim = self.var_sim.get()
        if not os.path.isfile(golden) and not sim:
            messagebox.showerror("Erreur", f"Golden absent : {golden}")
            return
        self.log_write(f"\n--- Vérif puce vs golden ({programmer}) ---")
        self._start(self._verify_chip_worker, programmer, golden, sim)

    def _verify_chip_worker(self, programmer, golden, sim):
        rc, _ = self._run_cmd(sc.flashrom_cmd("verify", programmer, golden), sim,
                              allow_fail=True)
        if sim:
            self._ui_queue.put(("done", "Simulation : vérif non effectuée."))
        elif rc == 0:
            self._ui_queue.put(("done", "Puce IDENTIQUE au golden ✔"))
        else:
            self._ui_queue.put(("error", "La puce DIFFÈRE du golden (voir journal)."))

    # ---------- Flash d'une unité ----------
    def _on_flash_unit(self):
        programmer = self._programmer()
        golden = self.golden_entry.get().strip()
        sim = self.var_sim.get()
        if not sim:
            if not os.path.isfile(golden):
                messagebox.showerror("Erreur", f"Golden absent : {golden}")
                return
            if not messagebox.askyesno(
                    "Confirmation",
                    f"Ceci EFFACE la SPI de l'unité via « {programmer} » et y écrit "
                    f"le golden.\n\nUne sauvegarde de la puce est faite avant.\n\n"
                    "Continuer ?"):
                return
        self.log_write(f"\n--- Flash unité ({programmer}) ---")
        self._start(self._flash_unit_worker, programmer, golden, sim)

    def _flash_unit_worker(self, programmer, golden, sim):
        # 1) Contrôle du golden (sauf en simulation pure sans fichier).
        if os.path.isfile(golden):
            with open(golden, "rb") as f:
                data = f.read()
            ok, reason = sc.looks_like_bootloader(data)
            if not ok:
                raise RuntimeError(f"Golden refusé : {reason}. Flash annulé.")
            sidecar = self._sha_sidecar(golden)
            if os.path.isfile(sidecar):
                want = open(sidecar, encoding="utf-8").read().split()[0]
                got = sc.sha256_bytes(data)
                if want.lower() != got.lower():
                    raise RuntimeError("SHA256 du golden != manifeste "
                                       f"({sidecar}). Flash annulé.")
                self.log_write("Golden conforme au manifeste SHA256.")
            if b"ethaddr=" in data:
                self.log_write("⚠ Golden avec MAC figée (ethaddr) : même MAC sur "
                               "toute la flotte. Voir DEPLOIEMENT_FLOTTE.md.")
        elif not sim:
            raise RuntimeError(f"Golden absent : {golden}")

        # 2) Sauvegarde pré-flash de la puce cible (jamais de flash sans filet).
        bdir = os.path.join(self._golden_dir, "preflash_backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = programmer.replace(":", "_").replace("/", "_")
        backup = os.path.join(bdir, f"preflash_{safe}_{stamp}.bin")
        self.log_write("Sauvegarde de la puce cible avant flash…")
        self._run_cmd(sc.flashrom_cmd("read", programmer, backup), sim)

        # 3) Écriture (flashrom vérifie après écriture par défaut).
        self._run_cmd(sc.flashrom_cmd("write", programmer, golden), sim)
        if sim:
            self._ui_queue.put(("done", "Simulation : flash non effectué."))
        else:
            self._ui_queue.put(("done", "Flash réussi et vérifié.\nSauvegarde "
                                        f"pré-flash : {backup}"))

    # ---------- Env U-Boot ----------
    def _on_env_apply(self):
        sim = self.var_sim.get()
        if not sim and not messagebox.askyesno(
                "Confirmation", "Écrire les 4 variables d'env U-Boot (mtd1) via "
                "fw_setenv ?"):
            return
        self.log_write("\n--- Application des 4 vars d'env ---")
        self._start(self._env_apply_worker, sim)

    def _env_apply_worker(self, sim):
        for argv in sc.fw_setenv_commands():
            self._run_cmd(argv, sim)
        self._ui_queue.put(("done", "Simulation : env non modifié." if sim
                            else "4 variables d'env appliquées (mtd1)."))

    def _on_env_dump(self):
        self.log_write("\n--- Sauvegarde de l'env (fw_printenv) ---")
        self._start(self._env_dump_worker, self.var_sim.get())

    def _env_dump_worker(self, sim):
        rc, out = self._run_cmd(["fw_printenv"], sim, allow_fail=True)
        if sim:
            self._ui_queue.put(("done", "Simulation : env non lu."))
            return
        if rc != 0:
            raise RuntimeError("fw_printenv a échoué (paquet u-boot-tools ? droits ? "
                               "/etc/fw_env.config manquant ?).")
        os.makedirs(self._golden_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._golden_dir,
                            f"uboot_env_{socket.gethostname()}_{stamp}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        if sc.env_has_ethaddr(out):
            self.log_write("⚠ ethaddr présent dans l'env : attention aux collisions "
                           "de MAC si on clone l'image complète sur la flotte.")
        self._ui_queue.put(("done", f"Env sauvegardé : {path}"))


def _fix_x11_env_for_sudo():
    """sudo n'hérite pas forcément du cookie X11 de l'utilisateur -> Tkinter ne
    peut pas se connecter au display. On repointe XAUTHORITY/DISPLAY vers ceux de
    SUDO_USER (même logique que clone_odroid_gui.py).

    Utile pour l'usage STANDALONE/dev (`sudo python3 spi_flash_gui.py` depuis une
    session X déjà ouverte). Inutile sur le poste kiosque (station_gui.py) : là,
    root démarre X lui-même via systemd (même utilisateur de bout en bout, pas de
    sudo dans la boucle)."""
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user:
        return
    try:
        import pwd
        home = pwd.getpwnam(sudo_user).pw_dir
    except Exception:
        return
    xauth = os.path.join(home, ".Xauthority")
    if os.path.isfile(xauth):
        os.environ["XAUTHORITY"] = xauth
    if "DISPLAY" not in os.environ:
        try:
            out = subprocess.run(
                ["bash", "-c", f"sudo -u {sudo_user} env | grep ^DISPLAY="],
                capture_output=True, text=True).stdout.strip()
            if out.startswith("DISPLAY="):
                os.environ["DISPLAY"] = out.split("=", 1)[1]
        except Exception:
            pass
        os.environ.setdefault("DISPLAY", ":0")


if __name__ == "__main__":
    if os.name != "posix":
        print("Outil Linux (Odroid ou PC/banc) : flashrom, fw_setenv, dmesg…")
        sys.exit(1)
    if os.geteuid() != 0:
        # Pas bloquant : la simulation marche sans root ; flasher / écrire l'env, non.
        print("⚠ Pas lancé en root : le flash et fw_setenv échoueront. "
              "Pour ces actions, relance avec : sudo python3 spi_flash_gui.py")

    _fix_x11_env_for_sudo()
    try:
        root = tk.Tk()
        root.title("SPI Odroid-M1 — golden / flash flotte / env")
        root.geometry("820x620")
        root.option_add("*Font", "TkDefaultFont 11")   # lisible au tactile
        SpiPanel(root).pack(fill="both", expand=True)
    except tk.TclError as e:
        print(f"Impossible d'ouvrir la fenêtre graphique : {e}")
        print("Essaie : sudo -E python3 spi_flash_gui.py (ou vérifie le X11 forwarding)")
        sys.exit(1)
    root.mainloop()
