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

Deux voies d'accès à la puce :
  - à la PINCE CH341A (programmer flashrom `ch341a_spi`, carte hors tension) —
    méthode master, marche même sur une carte vierge/briquée ;
  - SUR l'Odroid lui-même, à CHAUD (`MTD_PROGRAMMER`) : PAS via flashrom
    (`internal` n'existe pas sur le flashrom ARM64 d'apt, `linux_mtd:dev=N` ne
    lit qu'UNE partition), mais par réassemblage des partitions MTD
    (`/proc/mtd` + `/dev/mtdN`) qui pavent les 16 MiO. Lecture/vérif par
    concaténation, flash par `flashcp` partition par partition.
"""

import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time

import spi_core as sc

# Sentinelle : accès on-device à la puce SPI de CETTE machine, PAS via flashrom
# mais par réassemblage des partitions MTD (/proc/mtd + /dev/mtdN). C'est la
# voie « à chaud » : `flashrom -p internal` n'existe pas sur le flashrom ARM64
# d'apt, et `linux_mtd:dev=N` ne lit qu'UNE partition.
MTD_PROGRAMMER = "mtd"

# Méthodes proposées à l'interface : libellé lisible -> valeur passée aux
# opérations (chaîne flashrom `-p`, ou sentinelle MTD ci-dessus).
PROGRAMMERS = {
    "Pince CH341A — carte hors tension (méthode master)": "ch341a_spi",
    "Cette machine — puce SPI embarquée, à chaud (MTD)": MTD_PROGRAMMER,
}


def human_programmer(programmer):
    """Libellé lisible d'une valeur de programmer (pour les messages/confirms)."""
    if programmer == MTD_PROGRAMMER:
        return "cette machine (puce SPI embarquée, par-partition MTD)"
    for label, val in PROGRAMMERS.items():
        if val == programmer:
            return label
    return programmer


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
    def _finalize_golden(self, data, golden):
        """Valide un dump 16 MiO (taille/non-vierge/bannière U-Boot) puis écrit
        golden + manifeste SHA256, avec avertissement ethaddr. La validation
        précède TOUTE écriture : un dump refusé n'écrase pas un golden existant.
        Retourne le SHA256."""
        ok, reason = sc.looks_like_bootloader(data)
        if not ok:
            raise RuntimeError(f"Dump refusé : {reason}. Golden NON écrit.")
        digest = sc.sha256_bytes(data)
        os.makedirs(os.path.dirname(golden) or ".", exist_ok=True)
        with open(golden, "wb") as f:
            f.write(data)
        with open(sha_sidecar(golden), "w", encoding="utf-8") as f:
            f.write(f"{digest}  {os.path.basename(golden)}\n")
        if b"ethaddr=" in data:
            self.log("⚠ Le golden contient une MAC figée (ethaddr) : flasher "
                     "l'image complète donnerait la MÊME MAC à toute la flotte. "
                     "Voir DEPLOIEMENT_FLOTTE.md avant de flasher.")
        self.log(f"Golden écrit : {golden}\nSHA256 : {digest}")
        return digest

    def read_golden(self, programmer, golden):
        """Lit la puce -> fichier golden + manifeste SHA256. La lecture est
        validée (taille exacte, non vierge, bannière U-Boot) AVANT d'écraser un
        éventuel golden existant. Retourne le SHA256 (None en simulation).

        `programmer == MTD_PROGRAMMER` : lecture on-device par réassemblage des
        partitions MTD (à chaud, sans flashrom)."""
        if programmer == MTD_PROGRAMMER:
            return self.read_golden_mtd(golden)
        os.makedirs(os.path.dirname(golden) or ".", exist_ok=True)
        tmp = golden + ".read_tmp"
        self.run_cmd(sc.flashrom_cmd("read", programmer, tmp))
        if self.sim:
            return None
        with open(tmp, "rb") as f:
            data = f.read()
        os.remove(tmp)
        return self._finalize_golden(data, golden)

    def verify_chip(self, programmer, golden):
        """Compare la puce au golden. True = identique, False = diffère,
        None = simulation. `programmer == MTD_PROGRAMMER` : réassemble la puce
        depuis les partitions MTD et compare octet à octet (on-device)."""
        if programmer == MTD_PROGRAMMER:
            return self.verify_chip_mtd(golden)
        rc, _ = self.run_cmd(sc.flashrom_cmd("verify", programmer, golden),
                             allow_fail=True)
        if self.sim:
            return None
        return rc == 0

    # ---------- On-device : puce SPI de CETTE machine via MTD ----------
    def _read_mtd_device(self, dev, size):
        """Lit `size` octets de /dev/<dev> (partition MTD), en bouclant jusqu'à
        tout obtenir (une seule read() peut être courte)."""
        data = bytearray()
        with open(f"/dev/{dev}", "rb", buffering=0) as f:
            while len(data) < size:
                chunk = f.read(size - len(data))
                if not chunk:
                    break
                data.extend(chunk)
        if len(data) != size:
            raise RuntimeError(f"/dev/{dev} : lu {len(data)} octets != {size} "
                               "attendus (partition MTD tronquée ?).")
        return bytes(data)

    def _enumerate_mtd(self):
        """Énumère les partitions MTD de la machine : /proc/mtd (dev/size/name)
        + /sys/class/mtd/<dev>/offset. Repli : offsets cumulés dans l'ordre de
        /proc/mtd si sysfs ne les expose pas."""
        try:
            with open("/proc/mtd", encoding="utf-8") as f:
                parts = sc.parse_proc_mtd(f.read())
        except FileNotFoundError:
            raise RuntimeError("/proc/mtd absent : pas de sous-système MTD sur "
                               "cette machine (pas un Odroid, noyau sans MTD ?).")
        if not parts:
            raise RuntimeError("Aucune partition MTD listée dans /proc/mtd.")
        for p in parts:
            try:
                with open(f"/sys/class/mtd/{p['dev']}/offset", encoding="utf-8") as f:
                    p["offset"] = int(f.read().strip())
            except (OSError, ValueError):
                p["offset"] = None
        if any(p["offset"] is None for p in parts):
            running = 0
            for p in parts:      # repli : ordre de /proc/mtd = ordre des offsets
                p["offset"], running = running, running + p["size"]
        return parts

    def _read_mtd_full(self):
        """Réassemble l'image 16 MiO complète depuis les partitions MTD de CETTE
        machine (lecture directe de /dev/mtd*, aucun flashrom)."""
        ordered = sc.plan_full_readback(self._enumerate_mtd())
        self.log("Partitions MTD (réassemblage de la puce entière) :")
        for p in ordered:
            self.log(f"  {p['dev']}  offset={p['offset']:#010x}  "
                     f"taille={p['size']:#010x}  « {p['name']} »")
        buf = bytearray(sc.SPI_SIZE)
        for p in ordered:
            self.log(f"$ lecture /dev/{p['dev']} ({p['size']} octets)")
            buf[p["offset"]:p["offset"] + p["size"]] = \
                self._read_mtd_device(p["dev"], p["size"])
        return bytes(buf)

    def read_golden_mtd(self, golden):
        """Lit la puce SPI embarquée à CHAUD en réassemblant ses partitions MTD,
        puis valide et écrit le golden. Retourne le SHA256 (None en simulation)."""
        if self.sim:
            self.log("[SIMULATION] lecture /proc/mtd puis concat /dev/mtd* -> "
                     "golden (réassemblage on-device, sans flashrom)")
            return None
        return self._finalize_golden(self._read_mtd_full(), golden)

    def verify_chip_mtd(self, golden):
        """Réassemble la puce depuis les partitions MTD et la compare au golden
        octet à octet. True/False, None = simulation."""
        if self.sim:
            self.log("[SIMULATION] réassemblage MTD puis comparaison au golden")
            return None
        data = self._read_mtd_full()
        with open(golden, "rb") as f:
            want = f.read()
        same = data == want
        self.log("Puce IDENTIQUE au golden ✔" if same else
                 f"Puce DIFFÈRE du golden (puce {len(data)} o, "
                 f"golden {len(want)} o).")
        return same

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
        sauvegarde pré-flash (None en simulation sans fichier).

        `programmer == MTD_PROGRAMMER` : flash on-device par-partition
        (flashcp), à chaud sur la puce de CETTE machine."""
        if programmer == MTD_PROGRAMMER:
            return self.flash_unit_mtd(golden)
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

    def flash_unit_mtd(self, golden):
        """Flash on-device de la puce embarquée, partition par partition
        (flashcp). Contrôle le golden, sauvegarde la puce (réassemblage MTD),
        puis écrit chaque tranche du golden dans la partition correspondante.
        Retourne le chemin de la sauvegarde pré-flash (None en simulation)."""
        if os.path.isfile(golden):
            self.check_golden(golden)
        elif not self.sim:
            raise RuntimeError(f"Golden absent : {golden}")
        if self.sim:
            self.log("[SIMULATION] flash on-device par-partition — pour chaque "
                     "mtdN : flashcp -v <tranche du golden à l'offset de mtdN> "
                     "/dev/mtdN")
            return None

        ordered = sc.plan_full_readback(self._enumerate_mtd())
        with open(golden, "rb") as f:
            gdata = f.read()
        if len(gdata) != sc.SPI_SIZE:
            raise RuntimeError(f"Golden de {len(gdata)} octets != {sc.SPI_SIZE} "
                               "(16 MiO) — flash par-partition annulé.")

        # Sauvegarde pré-flash (réassemblage complet de la puce cible).
        bdir = os.path.join(self.golden_dir, "preflash_backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(bdir, f"preflash_mtd_{stamp}.bin")
        self.log("Sauvegarde de la puce cible avant flash (réassemblage MTD)…")
        with open(backup, "wb") as f:
            f.write(self._read_mtd_full())

        tmpdir = tempfile.mkdtemp(prefix="spi_flash_", dir=bdir)
        try:
            for p in ordered:
                off, size = p["offset"], p["size"]
                part_path = os.path.join(tmpdir, f"{p['dev']}.bin")
                with open(part_path, "wb") as f:
                    f.write(gdata[off:off + size])
                self.log(f"Flash de {p['dev']} « {p['name']} » "
                         f"({size} octets)…")
                self.run_cmd(sc.flashcp_cmd(part_path, f"/dev/{p['dev']}"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return backup

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
