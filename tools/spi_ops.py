#!/usr/bin/env python3
"""
spi_ops.py — opérations SPI partagées GUI/CLI (flashrom + fw_setenv), MÊME moteur.

Entre la logique pure (`spi_core`, testée sans matériel) et les interfaces
(onglet `spi_panel.SpiPanel`, sous-commandes `station.py spi …`) : l'exécution
réelle des commandes, avec les mêmes garde-fous des deux côtés — validation du
golden (taille/signature/manifeste SHA256), sauvegarde pré-flash systématique,
avertissement `ethaddr` (MAC figée). Sur le même principe que
`clone_engine.CloneEngine` pour le clonage : pas de duplication GUI/CLI.

`log` reçoit chaque ligne de journal ; `sim=True` journalise les commandes
exactes sans RIEN exécuter (revue de sécurité, sans matériel).

S'utilise aussi bien à la pince CH341A (programmer `ch341a_spi`, carte hors
tension) que SUR l'Odroid lui-même (`internal` / `linux_mtd:dev=0`) : lire ou
reflasher la puce SPI de la machine où le script tourne passe par `internal`.
"""

import os
import shlex
import socket
import subprocess
import time

import spi_core as sc

# Programmers proposés : libellé lisible -> chaîne passée à flashrom `-p`.
PROGRAMMERS = {
    "Pince CH341A (carte hors tension)": "ch341a_spi",
    "Cette machine — internal (puce SPI locale)": "internal",
    "Cette machine — linux_mtd:dev=0 (1 MTD, avancé)": "linux_mtd:dev=0",
}


def sha_sidecar(golden):
    return golden + ".sha256"


class SpiOps:
    """Une campagne d'opérations SPI = une instance (log + simulation figés)."""

    def __init__(self, log, golden_dir, sim=False):
        self.log = log
        self.golden_dir = golden_dir
        self.sim = sim

    # ---------- Exécution de commandes ----------
    def run_cmd(self, argv, allow_fail=False):
        """Exécute (ou simule) une commande. Retourne (returncode, sortie).

        En simulation : journalise la commande exacte, ne l'exécute pas, renvoie
        (0, ''). Sinon capture stdout+stderr et journalise la fin de sortie.
        """
        pretty = " ".join(shlex.quote(a) for a in argv)
        if self.sim:
            self.log(f"[SIMULATION] {pretty}")
            return 0, ""
        self.log(f"$ {pretty}")
        try:
            r = subprocess.run(argv, capture_output=True, text=True)
        except FileNotFoundError:
            raise RuntimeError(f"Commande introuvable : {argv[0]} "
                               "(paquet manquant ? flashrom / u-boot-tools)")
        out = (r.stdout or "") + (r.stderr or "")
        tail = "\n".join(out.splitlines()[-12:])
        if tail:
            self.log(tail)
        if r.returncode != 0 and not allow_fail:
            raise RuntimeError(f"Échec (code {r.returncode}) : {pretty}")
        return r.returncode, out

    # ---------- Golden : lecture / vérif ----------
    def read_golden(self, programmer, golden):
        """Lit la puce -> fichier golden + manifeste SHA256. La lecture est
        validée (taille exacte, non vierge, bannière U-Boot) AVANT d'écraser un
        éventuel golden existant. Retourne le SHA256 (None en simulation)."""
        os.makedirs(os.path.dirname(golden) or ".", exist_ok=True)
        tmp = golden + ".read_tmp"
        self.run_cmd(sc.flashrom_cmd("read", programmer, tmp))
        if self.sim:
            return None
        with open(tmp, "rb") as f:
            data = f.read()
        ok, reason = sc.looks_like_bootloader(data)
        if not ok:
            os.remove(tmp)
            raise RuntimeError(f"Dump refusé : {reason}. Golden NON écrit.")
        digest = sc.sha256_bytes(data)
        os.replace(tmp, golden)
        with open(sha_sidecar(golden), "w", encoding="utf-8") as f:
            f.write(f"{digest}  {os.path.basename(golden)}\n")
        if b"ethaddr=" in data:
            self.log("⚠ Le golden contient une MAC figée (ethaddr) : flasher "
                     "l'image complète donnerait la MÊME MAC à toute la flotte. "
                     "Voir DEPLOIEMENT_FLOTTE.md avant de flasher.")
        self.log(f"Golden écrit : {golden}\nSHA256 : {digest}")
        return digest

    def verify_chip(self, programmer, golden):
        """Compare la puce au golden. True = identique, False = diffère,
        None = simulation."""
        rc, _ = self.run_cmd(sc.flashrom_cmd("verify", programmer, golden),
                             allow_fail=True)
        if self.sim:
            return None
        return rc == 0

    # ---------- Flash d'une unité ----------
    def check_golden(self, golden):
        """Garde-fous AVANT tout flash : signature bootloader + manifeste SHA256
        + avertissement ethaddr. Lève si le golden est refusé."""
        with open(golden, "rb") as f:
            data = f.read()
        ok, reason = sc.looks_like_bootloader(data)
        if not ok:
            raise RuntimeError(f"Golden refusé : {reason}. Flash annulé.")
        sidecar = sha_sidecar(golden)
        if os.path.isfile(sidecar):
            want = open(sidecar, encoding="utf-8").read().split()[0]
            got = sc.sha256_bytes(data)
            if want.lower() != got.lower():
                raise RuntimeError("SHA256 du golden != manifeste "
                                   f"({sidecar}). Flash annulé.")
            self.log("Golden conforme au manifeste SHA256.")
        if b"ethaddr=" in data:
            self.log("⚠ Golden avec MAC figée (ethaddr) : même MAC sur "
                     "toute la flotte. Voir DEPLOIEMENT_FLOTTE.md.")

    def flash_unit(self, programmer, golden):
        """Contrôle le golden, sauvegarde la puce cible, écrit et vérifie
        (flashrom vérifie après écriture par défaut). Retourne le chemin de la
        sauvegarde pré-flash (None en simulation sans fichier)."""
        if os.path.isfile(golden):
            self.check_golden(golden)
        elif not self.sim:
            raise RuntimeError(f"Golden absent : {golden}")

        # Sauvegarde pré-flash de la puce cible (jamais de flash sans filet).
        bdir = os.path.join(self.golden_dir, "preflash_backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = programmer.replace(":", "_").replace("/", "_")
        backup = os.path.join(bdir, f"preflash_{safe}_{stamp}.bin")
        self.log("Sauvegarde de la puce cible avant flash…")
        self.run_cmd(sc.flashrom_cmd("read", programmer, backup))

        self.run_cmd(sc.flashrom_cmd("write", programmer, golden))
        return None if self.sim else backup

    # ---------- Env U-Boot (on-device) ----------
    def env_apply(self):
        """(Ré)applique les 4 variables d'env critiques via fw_setenv (mtd1)."""
        for argv in sc.fw_setenv_commands():
            self.run_cmd(argv)

    def env_dump(self):
        """Sauvegarde l'env U-Boot courant (fw_printenv) dans golden_dir.
        Retourne le chemin écrit (None en simulation)."""
        rc, out = self.run_cmd(["fw_printenv"], allow_fail=True)
        if self.sim:
            return None
        if rc != 0:
            raise RuntimeError("fw_printenv a échoué (paquet u-boot-tools ? "
                               "droits ? /etc/fw_env.config manquant ?).")
        os.makedirs(self.golden_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.golden_dir,
                            f"uboot_env_{socket.gethostname()}_{stamp}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        if sc.env_has_ethaddr(out):
            self.log("⚠ ethaddr présent dans l'env : attention aux collisions "
                     "de MAC si on clone l'image complète sur la flotte.")
        return path
