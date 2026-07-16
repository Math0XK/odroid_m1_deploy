#!/usr/bin/env python3
"""
clone_engine.py — MOTEUR du clonage de disque Odroid (sans interface).

Toute la mécanique à état du clonage (montages, rsync, mkfs, réécriture
d'identité, initramfs, audit de boot, progression) vit ici, derrière un
`report.Reporter` : l'onglet GUI (`clone_panel.ClonePanel`) et les
sous-commandes CLI (`station.py clone` / `station.py image`) pilotent LE MÊME
moteur — pas de duplication, mêmes garde-fous des deux côtés. La logique pure
sans état (blkid, sfdisk, patch boot.scr…) reste dans `clone_core.py`, les
contrôles de bootabilité dans `boot_audit.py` (testés sans matériel).

Le déroulé d'un clone (étapes numérotées dans le journal) :
  1. garde-fous (jamais le disque système) + lecture de la source ;
  2. table de partitions NEUVE (label-id régénéré, racine étendue) ;
  3. zone bootloader selon le mode de boot EXPLICITE (spi/disk) ;
  4. formatage aux MÊMES fs ET MÊMES FEATURES ext que la source (un mkfs
     récent activerait des features que le noyau 5.10 de l'unité refuse) ;
  5. montages + table de correspondance ancienne→nouvelle identité ;
  6. copie rsync (source figée read-only) ;
  7. réécriture de l'identité : fstab, partition BOOT, <root>/boot ET les
     configs qui régénèrent le boot (/etc/default/flash-kernel…) ;
  8. initramfs en chroot (ARM64) — le hook flash-kernel RÉGÉNÈRE alors
     boot.scr/uInitrd, c'est attendu ;
  9. CONTRÔLE FINAL : 2ᵉ passe de réécriture (rattrape les fichiers
     régénérés à l'étape 8), validation du boot.scr (root= résolu, cma
     préservé, CRC) avec restauration du boot.scr connu-bon si la
     régénération a produit un script cassé, puis AUDIT DE BOOT — NO-GO
     = le clonage est déclaré ÉCHOUÉ, le disque ne part pas en prod.

POURQUOI la double passe + l'audit : un clone réel a échoué (shell initramfs)
parce qu'`update-initramfs` dans le chroot déclenchait flash-kernel, qui
régénérait /boot/boot.scr depuis la config PÉRIMÉE du master (root=UUID
fantôme) APRÈS notre réécriture. Le master, lui, bootait toujours (son
boot.scr fait main n'avait jamais été régénéré). Voir boot_audit.py.
"""

import glob
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import time

import boot_audit
from clone_core import (
    BOOTLOADER_FIRST_SECTOR, blkid_value, bootloader_gap_present,
    build_dst_script, disk_label_id, dos_partuuid, extract_root_ids, fs_used,
    gen_label_id, image_size_bytes, is_system_mp, parse_ext_features,
    parse_start_size, part_name, rewrite_uboot_script, run, sfdisk_label_id,
    storage_modules,
)


# ---------------------------------------------------------------------------
# Helpers sans état (utilisés par le moteur ET par les pré-vérifications des
# interfaces, avant même de construire un moteur).
# ---------------------------------------------------------------------------
def disk_partitions(disk):
    """/dev/sda -> [/dev/sda1, ...] ; /dev/mmcblk0 -> [/dev/mmcblk0p1, ...]"""
    parts = glob.glob(disk + "[0-9]*") + glob.glob(disk + "p[0-9]*")
    return sorted(set(parts))


def all_mountpoints(device):
    """Points de montage RÉELS d'un périphérique, résolus par son numéro
    noyau (maj:min) dans /proc/self/mountinfo.

    On n'utilise SURTOUT PAS `findmnt --source <device>` : libmount y fait
    la correspondance par UUID/tag, pas par périphérique. Deux clones d'une
    même image partagent le même UUID (c'est précisément ce que ce script
    régénère), si bien que `findmnt --source /dev/sdX2` tombait sur le mount
    « / » d'un AUTRE disque au même UUID -> un simple disque USB était pris
    pour le disque système (faux « refusé en source »), et pire, le
    démontage forcé aurait pu viser « / ». Le numéro maj:min, lui, désigne
    un périphérique et un seul.
    """
    try:
        st = os.stat(device)
    except OSError:
        return []
    if not stat.S_ISBLK(st.st_mode):
        return []
    dev_no = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    mps = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                # champs mountinfo : 0=id 1=parent 2=maj:min 3=root 4=point…
                fields = line.split()
                if len(fields) > 4 and fields[2] == dev_no:
                    mps.append(fields[4])
    except OSError:
        pass
    return mps


def assert_not_system_disk(disk, role="destination"):
    """Refuse un disque portant le système en cours d'exécution.

    La détection s'appuie sur `all_mountpoints` (résolution par maj:min,
    pas par UUID) : un disque Odroid simplement branché en lecteur USB, même
    s'il partage l'UUID du disque hôte, n'est PAS pris pour le système. Il
    est auto-monté sous /media/... (ignoré ici : seuls /, /boot, /usr
    comptent) et reste clonable. Seul le VRAI disque système de la machine
    hôte est écarté — en source (clone à froid) comme en destination.
    """
    for part in [disk, *disk_partitions(disk)]:
        for mp in all_mountpoints(part):
            if is_system_mp(mp):
                raise RuntimeError(
                    f"{disk} porte le système en cours d'exécution "
                    f"({part} monté sur {mp}) : refusé en {role}.\n\n"
                    "Ce disque ne peut être ni cloné (clone à froid "
                    "uniquement : éteins l'Odroid et branche sa carte/eMMC "
                    "en lecteur USB) ni écrasé. Choisis un autre disque.")


# Configs du ROOTFS réécrites en plus de fstab/boot : elles alimentent la
# RÉGÉNÉRATION du boot (flash-kernel, initramfs-tools). Sans ça, le prochain
# `update-initramfs` (dans notre chroot à l'étape 8, ou plus tard sur l'unité
# à une mise à jour de kernel) régénérerait un boot.scr pointant vers les
# identifiants de la SOURCE -> racine introuvable au boot.
ROOTFS_REGEN_CONFIG_GLOBS = (
    "etc/default/flash-kernel",
    "etc/flash-kernel/*",
    "etc/flash-kernel/ubootenv.d/*",
    "etc/default/u-boot",
    "etc/initramfs-tools/conf.d/*",
)


class CloneEngine:
    """Un clonage = une instance. `reporter` est un `report.Reporter` (journal
    structuré + progression). `boot_mode` est « spi » (défaut, flotte) ou
    « disk » (legacy eMMC/SD auto-bootable) ; `boot_medium` est le disque du
    support de boot séparé legacy, ou None.

    Le clonage est TOUJOURS à froid : le disque système en marche est refusé en
    source comme en destination (`assert_not_system_disk`, garde-fou absolu).

    Trois points d'entrée : `clone()` (disque ou image -> disque),
    `make_image()` (disque -> fichier image COMPACT) et `restore_bundle()`
    (sauvegarde partclone -> disque). Chacun gère le losetup éventuel, le
    nettoyage (démontages, détachement loop) même en cas d'erreur, LÈVE en cas
    d'échec — y compris si l'AUDIT DE BOOT final est NO-GO — et retourne le
    message de fin.
    """

    def __init__(self, reporter, boot_mode="spi", boot_medium=None):
        if boot_mode not in ("spi", "disk"):
            raise ValueError(f"boot_mode inconnu : {boot_mode!r} (attendu 'spi' ou 'disk')")
        self.r = reporter
        self.boot_mode = boot_mode
        self.boot_medium = boot_medium
        self.loop_dev = None
        self._cleanup_mounts = []     # points de montage créés PAR NOUS (cleanup)
        self._poll_stop = None
        self._id_subs = {}
        self._feats_src = {}          # {"BOOT (p1)": {features}, "racine (p2)": …}
        self._feats_dst = {}
        self._bootscr_snapshot = None      # boot.scr patché, AVANT le chroot
        self._bootscr_expect_cma = False   # la source portait cma= dans boot.scr

    # ---------- Points d'entrée ----------
    def clone(self, src_disk, dst_disk, img_path=None):
        # 9 étapes de _do_clone + récapitulatif (+ support de boot legacy).
        self.r.begin(10 + (1 if self.boot_medium else 0))
        try:
            if img_path:
                self.r.info(f"Source = fichier image : {img_path} "
                            "(attachement en loop device…)")
                loop = run(["losetup", "--find", "--show", "-P", img_path]).strip()
                self.loop_dev = loop
                src_disk = loop
                self._wait_for_node(part_name(loop, 1))
                self.r.detail(f"image attachée sur {loop}")

            audit_go = self._do_clone(src_disk, dst_disk)
            self._recap(dst_disk, audit_go)
            if self.boot_medium:
                return (f"Clonage terminé — audit de boot : GO.\n\n"
                        f"Racine sur {dst_disk}, boot préparé sur "
                        f"{self.boot_medium}. Démarre l'ODROID avec le support "
                        f"de boot ({self.boot_medium}) + la cible ({dst_disk}) "
                        "branchés ; retire la source.")
            if self.boot_mode == "spi":
                return ("Clonage terminé — audit de boot : GO.\n\n"
                        "Le clone a sa propre identité (UUID/PARTUUID neufs ; "
                        "fstab, boot.scr, config flash-kernel et initramfs "
                        "réécrits et VÉRIFIÉS).\n\n"
                        "Rappel mode SPI : le disque ne boote que sur une carte "
                        "dont la puce SPI porte le golden (onglet SPI ou "
                        "odroid-station spi flash). Sur l'unité, termine par "
                        "odroid-station check.")
            return ("Clonage terminé — audit de boot : GO.\n\n"
                    "Identité neuve vérifiée (fstab, boot, initramfs). Mode "
                    "disque : règle l'ordre de boot (ou retire la source / la "
                    "SD) pour démarrer sur le clone.")
        finally:
            self._cleanup()

    def make_image(self, src_disk, img_path, margin=1.25):
        """Crée une IMAGE DISQUE COMPACTE de `src_disk` dans le fichier
        `img_path`, utilisable ensuite comme source de clonage (GUI « Fichier
        image » / CLI `--image`).

        Même pipeline que le clonage disque -> disque (`_do_clone` : identité
        neuve, réécritures, audit), mais la destination est un fichier attaché
        en loop device et DIMENSIONNÉ sur l'espace UTILISÉ de la racine source
        (`image_size_bytes`) : un NVMe 128 Go rempli à 20 % donne une image
        ~30 Go, pas 128. Fichier SPARSE de surcroît. En cas d'échec, le fichier
        partiel est SUPPRIMÉ (une image tronquée qui traîne finirait par servir
        de source de clonage).
        """
        # dimensionnement + 9 étapes de _do_clone + récapitulatif.
        self.r.begin(11)
        created = False
        try:
            self.r.step("Dimensionnement de l'image compacte")
            p2_start, root_used = self._measure_for_image(src_disk)
            size = image_size_bytes(p2_start, root_used, margin=margin)
            self.r.info(f"Racine source : {root_used / 1e9:.1f} Go utilisés -> "
                        f"image compacte de {size / 1e9:.1f} Go (marge "
                        "métadonnées incluse), fichier sparse.")
            with open(img_path, "wb") as f:
                f.truncate(size)               # sparse : aucun octet écrit
            created = True
            loop = run(["losetup", "--find", "--show", "-P", img_path]).strip()
            self.loop_dev = loop
            self.r.detail(f"image attachée en loop device : {loop}")

            audit_go = self._do_clone(src_disk, loop)

            # Détache AVANT la mesure finale : c'est le détachement qui garantit
            # que tout le cache du loop est retombé dans le fichier.
            run(["losetup", "-d", loop], check=False)
            self.loop_dev = None
            st = os.stat(img_path)
            real = st.st_blocks * 512
            self._recap(img_path, audit_go)
            self.r.info(f"Taille logique : {st.st_size / 1e9:.1f} Go — occupé "
                        f"réel (sparse) : {real / 1e9:.1f} Go.")
            return (f"Image compacte créée et AUDITÉE : {img_path}\n\n"
                    f"Taille logique {st.st_size / 1e9:.1f} Go, occupé réel "
                    f"{real / 1e9:.1f} Go (fichier sparse : préserver les trous "
                    "avec `cp --sparse=always` / `rsync -S`, ou compresser en "
                    ".zst pour la distribuer).\n\n"
                    "Utilisable comme SOURCE de clonage : onglet Clone -> "
                    "« Fichier image », ou odroid-station clone --image … "
                    "--dest /dev/nvme0n1 (la racine sera ré-étendue à la taille "
                    "de la cible).")
        except BaseException:
            if created:
                self._cleanup()          # détache le loop AVANT de supprimer
                try:
                    os.remove(img_path)
                    self.r.warn(f"Image partielle supprimée : {img_path}")
                except OSError:
                    pass
            raise
        finally:
            self._cleanup()

    def restore_bundle(self, bundle, dst_disk):
        """Restaure une sauvegarde bundle partclone (table sfdisk + images .pc)
        sur `dst_disk` (EFFACÉ), avec une IDENTITÉ NEUVE comme `clone()` :
        nouveaux label-id/UUID/PARTUUID, réécritures, initramfs, audit de boot.
        `bundle` provient de `clone_core.find_partclone_bundle`.

        Ne touche jamais la source (lecture de fichiers). Nécessite
        `partclone.restore` (paquet partclone) et e2fsprogs
        (`e2fsck`/`resize2fs`/`tune2fs`). LÈVE en cas d'échec, nettoie toujours.
        """
        if not shutil.which("partclone.restore"):
            raise RuntimeError("partclone.restore introuvable : installe le "
                               "paquet 'partclone' (sudo apt install partclone).")
        # 7 étapes de _do_restore_bundle + récapitulatif.
        self.r.begin(8)
        try:
            audit_go = self._do_restore_bundle(bundle, dst_disk)
            self._recap(dst_disk, audit_go)
            return ("Restauration terminée — audit de boot : GO.\n\n"
                    "Le disque a ses propres UUID/PARTUUID (identité régénérée, "
                    "racine étendue à la cible, boot vérifié).\n\n"
                    "Rappel mode SPI : il ne boote que sur une carte dont la "
                    "puce SPI porte le golden — flashe la SPI (onglet SPI), "
                    "puis vérifie avec odroid-station check.")
        finally:
            self._cleanup()

    # ---------- Récapitulatif ----------
    def _recap(self, target, audit_go):
        self.r.step("Récapitulatif")
        self.r.ok(f"Écriture terminée sur {target} — identité NEUVE "
                  "(aucune collision UUID/PARTUUID avec la source).")
        if audit_go:
            self.r.ok("Audit de boot : GO — root=, fstab, boot.scr, initramfs "
                      "et features vérifiés sur le clone.")
        for line in self.r.summary_lines():
            self.r.info(line)

    def _cleanup(self):
        """Nettoyage idempotent : arrêt du poll, démontages, détachement loop."""
        self._stop_progress_poll()
        for mp in reversed(self._cleanup_mounts):
            subprocess.run(["umount", mp], capture_output=True)
        self._cleanup_mounts = []
        if self.loop_dev:
            subprocess.run(["losetup", "-d", self.loop_dev], capture_output=True)
            self.loop_dev = None

    def _measure_for_image(self, src_disk):
        """(début de p2 en secteurs, octets utilisés de la racine source),
        mesurés sur un montage read-only éphémère (démonté aussitôt — _do_clone
        remontera la source proprement ensuite)."""
        src_p2 = part_name(src_disk, 2)
        sfdisk_out = run(["sfdisk", "-d", src_disk])
        p2_start, _ = parse_start_size(sfdisk_out, src_p2)
        mp = self._source_access(src_p2, "/mnt/clone_measure_root")
        try:
            return p2_start, fs_used(mp)
        finally:
            # ne démonte que ce que NOUS avons monté
            if mp in self._cleanup_mounts:
                subprocess.run(["umount", mp], capture_output=True)
                self._cleanup_mounts.remove(mp)

    # ---------- rsync ----------
    def _rsync(self, src, dst, dst_fstype="ext4", extra_excludes=None):
        """Copie par fichiers. La source étant un montage read-only figé (clone à
        froid), on exige un code de sortie 0 : plus de fichiers qui « bougent »
        pendant la copie, donc aucune tolérance aux transferts partiels."""
        excludes = [f"--exclude={e}" for e in (extra_excludes or [])]
        if dst_fstype == "vfat":
            # FAT ne gère ni propriétaires, ni liens, ni ACL : -a échouerait.
            attempts = [["-rt", "--modify-window=2"]]
        else:
            # --numeric-ids : indispensable quand on clone depuis un PC dont les
            # uid/gid ne correspondent pas à ceux de l'Odroid (sinon rsync
            # remappe les propriétaires par NOM et le clone ne boote plus).
            attempts = [["-aHAX", "--numeric-ids"], ["-aH", "--numeric-ids"]]

        last = None
        for flags in attempts:
            r = subprocess.run(["rsync", *flags, *excludes, src, dst],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return
            last = r
            self.r.detail(f"rsync {' '.join(flags)} a échoué "
                          f"(code {r.returncode}), tentative suivante…")
        raise RuntimeError(f"rsync a échoué (code {last.returncode}):\n{last.stderr[-3000:]}")

    # ---------- Périphériques / montages ----------
    def _force_unmount(self, device):
        """Démonte un périphérique de partout (auto-montage GNOME/udisks2
        compris). Utilisé pour la destination (avant écrasement) et pour la
        source (avant remontage read-only) — jamais sur le disque système, écarté
        en amont par `assert_not_system_disk`.

        Filet de sécurité : on ne démonte JAMAIS /, /boot ou /usr, quoi qu'il
        arrive — même si une détection tournait mal, on ne peut pas casser le
        système hôte en essayant de démonter sa racine.
        """
        subprocess.run(["udisksctl", "unmount", "-b", device], capture_output=True)
        for flag in ([], ["-l"]):
            for mp in all_mountpoints(device):
                if is_system_mp(mp):
                    continue
                subprocess.run(["umount", *flag, mp], capture_output=True)

    def _mount(self, device, mountpoint, ro=False, fstype=None):
        os.makedirs(mountpoint, exist_ok=True)
        if not ro:
            # destination : évince un éventuel auto-montage udisks2 survenu
            # entre le formatage et ce montage
            self._force_unmount(device)
        subprocess.run(["umount", mountpoint], capture_output=True)
        cmd = ["mount"]
        if fstype:
            cmd += ["-t", fstype]
        if ro:
            cmd += ["-o", "ro"]
        cmd += [device, mountpoint]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            # joint la fin du dmesg : le noyau y explique souvent le refus
            # (UUID dupliqué, feature non supportée...), que mount ne dit pas.
            dmesg = subprocess.run(["dmesg"], capture_output=True, text=True)
            tail = "\n".join(dmesg.stdout.splitlines()[-8:])
            raise RuntimeError(f"Commande échouée: {' '.join(cmd)}\n{r.stderr}\n"
                               f"--- dernières lignes du noyau (dmesg) ---\n{tail}")
        self._cleanup_mounts.append(mountpoint)

    def _source_access(self, part, fallback_mp):
        """Chemin de lecture d'une partition source (clone à froid).

        La source n'est jamais le système en marche (garde-fou en amont) : on
        démonte son éventuel auto-montage (udisks2, en lecture-écriture) et on la
        remonte read-only pour lire un instantané cohérent et figé.
        """
        self._force_unmount(part)
        self._mount(part, fallback_mp, ro=True,
                    fstype=blkid_value(part, "TYPE") or None)
        return fallback_mp

    def _wait_for_node(self, path, timeout=5.0):
        """Attend l'apparition d'un nœud de partition (udev peut être lent
        après sfdisk/losetup -P)."""
        subprocess.run(["partprobe"], capture_output=True)
        run(["udevadm", "settle"], check=False)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path):
                return
            time.sleep(0.2)
        raise RuntimeError(f"Partition {path} jamais apparue (udev/partprobe).")

    # ---------- Bootloader (zone brute hors partition) ----------
    def _read_sector64(self, disk):
        """512 octets du secteur 64 (offset 0x8000) d'un disque, ou b'' si
        illisible. Sert à repérer un idbloader résiduel (informatif)."""
        try:
            with open(disk, "rb") as f:
                f.seek(BOOTLOADER_FIRST_SECTOR * 512)
                return f.read(512)
        except OSError:
            return b""

    def _copy_bootloader_gap(self, src_disk, dst_disk, p1_start):
        """Zone bootloader brute (secteurs 64 -> début p1) selon le MODE DE BOOT
        EXPLICITE — jamais déduit du contenu du disque.

        - mode « spi » (défaut, flotte) : le boot passe par la puce SPI + u-boot ;
          le disque n'est PAS le chemin de boot. On NE recopie rien et on ne fait
          AUCUNE vérif secteur-64 (qui ferait échouer le clone à tort). Un
          idbloader résiduel (vestige d'un ancien clone disque-boot) est signalé
          comme sans effet.
        - mode « disk » (legacy eMMC/SD) : comportement historique — copie dd de
          idbloader.img (secteur 64) + u-boot.itb (secteur 16384), hors partition
          donc invisibles pour rsync, puis vérification du secteur 64.
        """
        self.r.step("Zone bootloader — mode de boot : "
                    + ("SPI (flotte)" if self.boot_mode == "spi" else "disque (legacy)"))
        if self.boot_mode == "spi":
            if bootloader_gap_present(self._read_sector64(src_disk)):
                self.r.info(
                    "Idbloader résiduel au secteur 64 de la source (vestige "
                    "d'un ancien clone disque-boot) — laissé de côté, SANS "
                    "effet : le boot passe par la puce SPI + u-boot.")
            else:
                self.r.info("Pas de bootloader sur le disque (normal en mode "
                            "SPI) : zone bootloader ignorée.")
            self.r.detail("le clone ne bootera que sur une carte dont la SPI "
                          "porte le golden (onglet SPI / odroid-station spi flash)")
            return

        count = p1_start - BOOTLOADER_FIRST_SECTOR
        if count <= 0:
            self.r.warn(f"Partition 1 démarre au secteur {p1_start} "
                        f"(< {BOOTLOADER_FIRST_SECTOR}) : pas de zone "
                        "bootloader à copier. Clone probablement non "
                        "bootable en démarrage direct SD/eMMC.")
            return
        kib = count * 512 // 1024
        taille = f"{kib} Ko" if kib < 1024 else f"{kib / 1024:.1f} Mo"
        self.r.info(f"Copie du bootloader (secteurs {BOOTLOADER_FIRST_SECTOR} "
                    f"-> {p1_start} : idbloader + u-boot, {taille})…")
        r = subprocess.run(
            ["dd", f"if={src_disk}", f"of={dst_disk}", "bs=512",
             f"skip={BOOTLOADER_FIRST_SECTOR}", f"seek={BOOTLOADER_FIRST_SECTOR}",
             f"count={count}", "conv=fsync"],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Copie du bootloader échouée (dd) :\n{r.stderr}")
        # Contrôle : la signature idbloader Rockchip doit se retrouver sur la
        # destination (le BootROM la cherche au secteur 64).
        if self._read_sector64(dst_disk) != self._read_sector64(src_disk):
            raise RuntimeError("Vérification bootloader échouée : le secteur 64 "
                               "de la destination diffère de la source.")
        self.r.ok("Bootloader copié et vérifié (secteur 64 identique).")

    # ---------- mkfs ----------
    def _ext_features_of(self, part):
        """Features ext* d'une partition via `dumpe2fs -h` (ensemble vide si
        fs non-ext ou dumpe2fs indisponible)."""
        r = subprocess.run(["dumpe2fs", "-h", part], capture_output=True, text=True)
        return parse_ext_features(r.stdout) if r.returncode == 0 else set()

    def _mkfs_like_source(self, src_part, dst_part, role):
        """Formate la destination avec le MÊME type de fs — ET LES MÊMES
        FEATURES ext — que la source, mais avec un UUID FRAIS.

        Mêmes features : un e2fsprogs récent sur le poste de clonage active par
        défaut des features (`orphan_file`, `metadata_csum_seed`…) que le noyau
        vendor 5.10 de l'unité ou un u-boot ancien peuvent REFUSER au montage.
        On lit donc les features de la source (`dumpe2fs -h`) et on les impose
        au mkfs (`-O liste`, qui REMPLACE les défauts) : le clone est
        montable partout où la source l'était. Si le mkfs local ne connaît pas
        une feature, on retombe sur les défauts avec avertissement (l'audit
        final signalera un éventuel écart risqué).

        UUID frais : on ne recopie PAS l'UUID de la source (deux fs au même
        UUID branchés ensemble -> le noyau/systemd peut monter le mauvais).
        La cohérence fstab/boot est rétablie ensuite par
        `_rewrite_clone_identity`. Le label (nom de volume) est conservé.
        """
        fstype = blkid_value(src_part, "TYPE")
        label = blkid_value(src_part, "LABEL")
        feats = set()
        if fstype == "vfat":
            cmd = ["mkfs.vfat"]
            if label:
                cmd += ["-n", label[:11]]
            cmd.append(dst_part)
        elif fstype in ("ext2", "ext3", "ext4"):
            feats = self._ext_features_of(src_part)
            self._feats_src[role] = feats
            cmd = [f"mkfs.{fstype}", "-F", "-q"]
            if label:
                cmd += ["-L", label]
            if feats:
                cmd += ["-O", ",".join(sorted(feats))]
            cmd.append(dst_part)
        else:
            raise RuntimeError(f"Type de système de fichiers non géré sur "
                               f"{src_part} : {fstype or 'inconnu'}")
        self.r.info(f"Formatage {dst_part} — {fstype}, UUID frais"
                    + (f", {len(feats)} features miroir de la source" if feats else ""))
        # udisks2/GNOME peut auto-monter la partition sitôt créée : on démonte
        # juste avant de formater (mkfs refuse un fs monté).
        self._force_unmount(dst_part)
        try:
            run(cmd)
        except RuntimeError:
            if not feats:
                raise
            # mkfs local trop vieux pour une feature de la source : défauts +
            # avertissement (l'audit final comparera les features réelles).
            self.r.warn(f"mkfs a refusé la liste de features de la source sur "
                        f"{dst_part} : formatage avec les défauts du poste. "
                        "L'audit final contrôlera la compatibilité.")
            cmd = [c for c in cmd if c != "-O" and c != ",".join(sorted(feats))]
            run(cmd)
        if fstype in ("ext2", "ext3", "ext4"):
            self._feats_dst[role] = self._ext_features_of(dst_part)
        return fstype

    # ---------- Divergence UUID du boot.scr ----------
    def _augment_subs_from_bootscr(self, src_boot, src_root, new_ids):
        """Ajoute à `self._id_subs` tout identifiant que le `boot.scr` de la
        source désigne comme racine mais qui est ABSENT de blkid (divergence
        connue sur ce projet : la config flash-kernel du master traîne un
        root=UUID périmé). Chacun est mappé vers l'identité NEUVE du clone du
        même type, à longueur constante (donc patchable par
        `rewrite_uboot_script`). Mémorise aussi si la source exigeait `cma=`
        dans ses bootargs (le NPU en dépend) — l'audit final le vérifiera.
        """
        candidates = [os.path.join(src_boot, "boot.scr"),
                      os.path.join(src_root, "boot", "boot.scr")]
        scr = next((p for p in candidates if os.path.isfile(p)), None)
        if not scr:
            self.r.warn("Aucun boot.scr trouvé sur la source : la validation "
                        "cma/root= du boot sera partielle.")
            return
        try:
            with open(scr, "rb") as f:
                data = f.read()
        except OSError:
            return
        body = data[64:] if data[:4] == b"\x27\x05\x19\x56" else data
        self._bootscr_expect_cma = b"cma=" in body
        known = {k.lower() for k in self._id_subs}
        for kind, val in extract_root_ids(data):
            if val.lower() in known:
                continue                       # déjà couvert par blkid
            new = new_ids["puid_p2"] if kind == "PARTUUID" else new_ids["uuid_p2"]
            if not new:
                continue
            self._id_subs[val] = new
            known.add(val.lower())
            self.r.warn(
                f"Le boot.scr source désigne la racine par {kind}={val}, "
                "identifiant ABSENT de blkid (config flash-kernel périmée sur "
                f"le master ?). Remappé vers l'identité neuve du clone ({new}) "
                "— à assainir sur le master (docs/DEPLOIEMENT_FLOTTE.md §4).")

    # ---------- Réécriture de l'identité (fstab + boot + configs regen) ----------
    def _rewrite_clone_identity(self, subs, root_mp, boot_mp):
        """Remplace, DANS LA CONFIG DU CLONE, chaque identifiant de la source
        par celui du clone (table `subs` : ancien -> nouveau). Retourne la
        liste des fichiers modifiés (vide si rien ne référençait la source).

        Sans ça, le clone -- qui porte de NOUVEAUX UUID/PARTUUID -- garderait un
        fstab et une config bootloader (extlinux.conf/boot.ini/armbianEnv.txt,
        cmdline...) pointant encore vers les UUID de la SOURCE : le noyau ne
        trouverait pas sa racine, ou monterait la partition de la source.

        Balaie /etc/fstab, les CONFIGS DE RÉGÉNÉRATION du boot
        (ROOTFS_REGEN_CONFIG_GLOBS : /etc/default/flash-kernel…, sans quoi le
        prochain update-initramfs régénérerait un boot.scr pointant sur la
        source) et tout fichier de la partition BOOT et de <root>/boot. Les
        fichiers TEXTE sont réécrits directement. Les **images script u-boot**
        (`boot.scr`, magic 0x27051956) — binaires mais qui embarquent
        typiquement `root=UUID=…` sur ODROID — sont patchées en place puis
        leurs CRC refaits (voir `rewrite_uboot_script`). Les autres binaires
        (noyau, dtb, initrd) sont ignorés : un remplacement naïf les
        corromprait. IDEMPOTENT : une 2ᵉ passe ne change que ce qui a été
        régénéré entre-temps.
        """
        if not subs:
            self.r.warn("Aucune correspondance d'identité à réécrire "
                        "(UUID source illisibles ?).")
            return []
        # clés les plus longues d'abord : un PARTUUID (label-id + suffixe)
        # doit être remplacé avant le label-id nu qu'il contient.
        ordered = sorted(subs.items(), key=lambda kv: -len(kv[0]))

        targets = [os.path.join(root_mp, "etc", "fstab")]
        for pat in ROOTFS_REGEN_CONFIG_GLOBS:
            targets.extend(p for p in glob.glob(os.path.join(root_mp, pat))
                           if os.path.isfile(p))
        for base in (boot_mp, os.path.join(root_mp, "boot")):
            for dirpath, _dirs, files in os.walk(base):
                for fn in files:
                    targets.append(os.path.join(dirpath, fn))

        changed = []
        for path in targets:
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                if os.path.getsize(path) > 1_000_000:
                    continue
                with open(path, "rb") as f:
                    data = f.read()

                # Image script u-boot (boot.scr, magic 0x27051956) : binaire, donc
                # invisible pour la voie texte -- or c'est justement là que vit
                # souvent `root=UUID=…` sur ODROID (compilée par mkimage). On la
                # patche en place et on recalcule ses CRC (u-boot rejette une
                # image au CRC faux).
                if data[:4] == b"\x27\x05\x19\x56":
                    patched = rewrite_uboot_script(data, ordered)
                    if patched is not None and patched != data:
                        with open(path, "wb") as f:
                            f.write(patched)
                        changed.append(path + " (image u-boot, CRC recalculés)")
                    continue

                if b"\x00" in data:            # autre binaire -> on ne touche pas
                    continue
                text = data.decode("utf-8", "surrogateescape")
                new = text
                for old, repl in ordered:
                    new = re.sub(re.escape(old), repl, new, flags=re.IGNORECASE)
                if new != text:
                    with open(path, "wb") as f:  # même inode -> droits préservés
                        f.write(new.encode("utf-8", "surrogateescape"))
                    changed.append(path)
            except OSError:
                continue
        return changed

    def _log_rewrites(self, changed, context):
        if changed:
            self.r.ok(f"{len(changed)} fichier(s) réécrit(s) vers l'identité "
                      f"du clone ({context}) :")
            for p in changed:
                self.r.detail(p)
        else:
            self.r.info(f"Aucun fichier à réécrire ({context}).")

    # ---------- Initramfs + régénération du boot (chroot) ----------
    def _rebuild_initramfs(self, root_mp, boot_mp):
        """Reconstruit l'initramfs du clone (chroot + update-initramfs) en y
        ajoutant les pilotes du disque cible.

        Nécessite un chroot de MÊME architecture : ARM64 -> il faut tourner sur
        l'ODROID (ou un autre ARM64). Sur un hôte x86, on saute proprement en
        indiquant la commande à lancer plus tard sur l'ODROID.

        EFFET DE BORD CONNU ET GÉRÉ : sur les images Hardkernel/Debian,
        `update-initramfs` déclenche le hook **flash-kernel**, qui RÉGÉNÈRE
        /boot/boot.scr (et uInitrd) depuis /etc/default/flash-kernel — config
        réécrite vers l'identité du clone à l'étape précédente, et le résultat
        est re-contrôlé (2ᵉ passe + validation + audit) à l'étape suivante.
        C'est ce hook qui, avant ce correctif, écrasait le boot.scr patché avec
        le root=UUID périmé du master -> clone en shell (initramfs).
        """
        if platform.machine() not in ("aarch64", "arm64"):
            self.r.warn(
                f"Initramfs NON reconstruit (hôte {platform.machine()}, pas "
                "ARM64). À faire sur l'ODROID une fois le clone en place :\n"
                "    sudo update-initramfs -u   (avec les modules NVMe/PCIe)")
            return
        modfile = os.path.join(root_mp, "etc", "initramfs-tools", "modules")
        if not os.path.isfile(modfile):
            self.r.warn("Pas d'initramfs-tools dans le clone : rien à "
                        "reconstruire (image sans initramfs Debian/Ubuntu ?).")
            return
        mods = storage_modules()
        try:
            with open(modfile, "r", encoding="utf-8") as f:
                existing = {ln.strip() for ln in f}
            to_add = [m for m in mods if m not in existing]
            if to_add:
                with open(modfile, "a", encoding="utf-8") as f:
                    f.write("\n# pilotes du disque cible (clone_engine)\n")
                    f.write("\n".join(to_add) + "\n")
        except OSError as e:
            self.r.warn(f"Impossible d'écrire {modfile} : {e}")
            return

        self.r.info(f"Reconstruction de l'initramfs (modules cible : "
                    f"{', '.join(mods) or 'aucun'})…")
        if os.path.exists(os.path.join(root_mp, "usr", "sbin", "flash-kernel")):
            self.r.info("flash-kernel présent dans le clone : il va régénérer "
                        "boot.scr/uInitrd — le contrôle final re-vérifie tout.")
        # Le boot du clone doit être visible sous <root>/boot pour que
        # update-initramfs y écrive le nouvel initrd.
        binds = []
        try:
            for src, sub in ((boot_mp, "boot"), ("/dev", "dev"),
                             ("/proc", "proc"), ("/sys", "sys")):
                dst = os.path.join(root_mp, sub)
                os.makedirs(dst, exist_ok=True)
                subprocess.run(["mount", "--bind", src, dst],
                               check=True, capture_output=True)
                binds.append(dst)
            self.r.cmd("chroot <clone> update-initramfs -u -k all")
            r = subprocess.run(["chroot", root_mp, "update-initramfs", "-u", "-k", "all"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                self.r.warn("update-initramfs a échoué (le clone risque de ne "
                            "pas voir sa racine au boot) :\n"
                            + (r.stderr or r.stdout)[-1500:])
            else:
                self.r.ok("Initramfs reconstruit : le clone embarque les "
                          "pilotes de son disque cible.")
        except Exception as e:
            self.r.warn(f"Reconstruction initramfs impossible : {e}")
        finally:
            for dst in reversed(binds):
                subprocess.run(["umount", dst], capture_output=True)

    # ---------- Contrôle final : validation du boot.scr + audit ----------
    def _snapshot_bootscr(self, root_mp, boot_mp):
        """Copie en mémoire du boot.scr du clone APRÈS la 1ʳᵉ réécriture
        d'identité (donc connu-bon : bootargs de la source validés à la main +
        identifiants du clone). Sert de filet si la régénération flash-kernel
        de l'étape initramfs produit un script cassé."""
        scr = boot_audit.find_bootscr(boot_mp, root_mp)
        if scr:
            try:
                with open(scr, "rb") as f:
                    self._bootscr_snapshot = f.read()
            except OSError:
                self._bootscr_snapshot = None

    def _validate_final_bootscr(self, root_mp, boot_mp, valid_ids):
        """Le boot.scr FINAL (possiblement régénéré par flash-kernel) doit :
        avoir des CRC valides, désigner une racine qui EXISTE sur le clone, et
        garder `cma=` si la source l'exigeait. Sinon on restaure le snapshot
        connu-bon (bootargs de la source + identité du clone)."""
        scr = boot_audit.find_bootscr(boot_mp, root_mp)
        if scr is None:
            if self._bootscr_snapshot:
                dest = os.path.join(boot_mp, "boot.scr")
                with open(dest, "wb") as f:
                    f.write(self._bootscr_snapshot)
                self.r.warn("boot.scr disparu après la régénération : restauré "
                            f"depuis la copie connue-bonne ({dest}).")
            return
        try:
            with open(scr, "rb") as f:
                data = f.read()
        except OSError:
            return
        body = data[64:] if data[:4] == b"\x27\x05\x19\x56" else data
        problems = []
        if not boot_audit.uimage_crc_ok(data):
            problems.append("CRC uImage invalides")
        bad = [f"{k}={v}" for k, v in extract_root_ids(data)
               if v.lower() not in valid_ids]
        if bad:
            problems.append(f"root= étranger au clone ({', '.join(bad)})")
        if self._bootscr_expect_cma and b"cma=" not in body:
            problems.append("cma= perdu (bootargs régénérés sans le fix NPU)")
        if not problems:
            self.r.ok("boot.scr final : CRC valides, root= résolu sur le clone"
                      + (", cma= préservé" if self._bootscr_expect_cma else "") + ".")
            return
        if self._bootscr_snapshot:
            with open(scr, "wb") as f:
                f.write(self._bootscr_snapshot)
            self.r.warn("boot.scr régénéré (flash-kernel) DÉFECTUEUX — "
                        + " ; ".join(problems) + ". Restauré depuis la copie "
                        "connue-bonne (bootargs de la source + identité du clone).")
        else:
            self.r.error("boot.scr final défectueux (" + " ; ".join(problems)
                         + ") et aucune copie de secours : l'audit va trancher.")

    def _clone_valid_ids(self, parts):
        """Toutes les valeurs UUID/PARTUUID (minuscules) présentes sur le
        clone — la référence contre laquelle l'audit résout root=/fstab."""
        ids = set()
        for part in parts:
            for tag in ("UUID", "PARTUUID"):
                v = blkid_value(part, tag)
                if v:
                    ids.add(v.lower())
        return ids

    def _run_boot_audit(self, root_mp, boot_mp, parts):
        """Audit de boot final (boot_audit.run_audit) : journalise chaque
        contrôle, retourne True si GO, LÈVE si NO-GO — un clone qui ne
        trouvera pas sa racine ne doit JAMAIS être déclaré réussi."""
        self.r.info("Audit de boot du clone (le disque doit pouvoir trouver "
                    "sa racine au premier essai) :")
        valid_ids = self._clone_valid_ids(parts)
        expect = ("cma=128M",) if self._bootscr_expect_cma else ()
        checks = boot_audit.run_audit(
            root_mp, boot_mp, valid_ids, expect_tokens=expect,
            src_feats=self._feats_src or None, dst_feats=self._feats_dst or None)
        for c in checks:
            if c.ok:
                self.r.ok(f"{c.name} — {c.msg}")
            elif c.severity == boot_audit.SEV_FAIL:
                self.r.error(f"{c.name} — {c.msg}")
            else:
                self.r.warn(f"{c.name} — {c.msg}")
        go, n_fail, n_warn = boot_audit.verdict(checks)
        if go:
            self.r.ok(f"AUDIT DE BOOT : GO ({len(checks)} contrôles"
                      + (f", {n_warn} avertissement(s)" if n_warn else "") + ")")
            return True
        raise RuntimeError(
            f"AUDIT DE BOOT : NO-GO — {n_fail} contrôle(s) bloquant(s) en échec "
            "(détail dans le journal ci-dessus). Ce disque ne trouverait pas sa "
            "racine au boot : il n'est PAS déployable en l'état. Corrige la "
            "cause puis relance le clonage.")

    def _finalize_boot(self, root_mp, boot_mp, parts):
        """Étape « contrôle final » complète : 2ᵉ passe de réécriture
        (rattrape ce que flash-kernel a régénéré), validation/restauration du
        boot.scr, puis audit de boot. Retourne True (GO) ou lève."""
        changed = self._rewrite_clone_identity(self._id_subs, root_mp, boot_mp)
        if changed:
            self.r.info("Fichiers régénérés à l'étape initramfs, re-réécrits "
                        "vers l'identité du clone :")
            for p in changed:
                self.r.detail(p)
        valid_ids = self._clone_valid_ids(parts)
        self._validate_final_bootscr(root_mp, boot_mp, valid_ids)
        return self._run_boot_audit(root_mp, boot_mp, parts)

    # ---------- Support de boot séparé (USB/SD) ----------
    def _prepare_boot_medium(self, boot_disk, dst_boot_mp):
        """Efface `boot_disk` et y écrit une partition de boot unique, copie du
        boot du clone (kernel + initrd reconstruit + boot.scr déjà réglé sur
        l'UUID de la cible). u-boot lit ce support (USB/SD), la racine reste sur
        la cible — indispensable quand la cible ne boote pas seule (SSD USB-SATA
        en pont UAS qu'u-boot ne pilote pas ; un NVMe boote seul via la SPI).
        """
        assert_not_system_disk(boot_disk, role="support de boot")
        self.r.step(f"Support de boot séparé (legacy) : {boot_disk} — EFFACÉ")
        for part in disk_partitions(boot_disk):
            self._force_unmount(part)
        self._force_unmount(boot_disk)
        run(["wipefs", "-a", boot_disk])
        run(["parted", "--script", boot_disk, "mklabel", "msdos"])
        run(["parted", "--script", "--align", "optimal", boot_disk,
             "mkpart", "primary", "ext2", "1MiB", "1025MiB"])
        run(["parted", "--script", boot_disk, "set", "1", "boot", "on"])
        p1 = part_name(boot_disk, 1)
        self._wait_for_node(p1)
        run(["mkfs.ext2", "-F", "-L", "BOOT", p1])
        self._mount(p1, "/mnt/clone_bootmedium", fstype="ext2")
        self._rsync(f"{dst_boot_mp}/", "/mnt/clone_bootmedium/", dst_fstype="ext2")
        self.r.ok(f"Support de boot prêt : démarre l'ODROID sur {boot_disk} "
                  "(la racine reste sur la cible).")

    # ---------- Progression ----------
    def _start_progress_poll(self, src_paths, dst_paths):
        """Suit la copie en comparant l'espace ÉCRIT sur la destination à
        l'espace utilisé de la source (statvfs) : pourcentage global fiable,
        sans dépendre du format de sortie de rsync."""
        total = sum(fs_used(p) for p in src_paths)
        # la destination fraîchement formatée n'est pas à 0 (journal, métadonnées) :
        # on retranche ce plancher pour partir de 0 %
        base = sum(fs_used(p) for p in dst_paths)
        # Event capturé dans une LOCALE (`stop`) utilisée par le thread : sinon,
        # au moment où _stop_progress_poll fait `.set()` puis remet
        # `self._poll_stop = None`, le thread pouvait relire `self._poll_stop`
        # devenu None entre deux tours -> `None.is_set()` (AttributeError).
        stop = threading.Event()
        self._poll_stop = stop

        def loop():
            while not stop.is_set():
                done = 0
                for p in dst_paths:
                    try:
                        done += fs_used(p)
                    except OSError:
                        pass
                pct = 0.0
                if total > base:
                    pct = max(0.0, min(99.0, 100.0 * (done - base) / (total - base)))
                # 15 -> 88 % : la copie est le gros de l'opération, mais les
                # étapes d'avant/après gardent leur part de barre.
                scaled = 15.0 + pct * 0.73
                self.r.progress(scaled, f"Copie des données… {pct:.0f} %")
                stop.wait(0.5)

        threading.Thread(target=loop, daemon=True).start()

    def _stop_progress_poll(self):
        if self._poll_stop is not None:
            self._poll_stop.set()
            self._poll_stop = None

    # ---------- Cœur du clonage ----------
    def _do_clone(self, src_disk, dst_disk):
        r = self.r
        r.step("Garde-fous et lecture de la source")
        r.progress(2, "Préparation…")

        src_parts = disk_partitions(src_disk)
        if len(src_parts) != 2:
            raise RuntimeError(f"Source inattendue : 2 partitions attendues "
                               f"(BOOT + rootfs), trouvé {len(src_parts)} sur {src_disk}.")
        src_p1, src_p2 = part_name(src_disk, 1), part_name(src_disk, 2)
        dst_p1, dst_p2 = part_name(dst_disk, 1), part_name(dst_disk, 2)

        # Démonte tout auto-montage de la destination avant de l'écraser
        # (la source sera démontée puis remontée read-only par _source_access).
        for part in disk_partitions(dst_disk):
            self._force_unmount(part)

        sfdisk_out = run(["sfdisk", "-d", src_disk])
        p1_start, _ = parse_start_size(sfdisk_out, src_p1)
        p2_start, _ = parse_start_size(sfdisk_out, src_p2)
        r.info(f"Source {src_disk} : BOOT ({src_p1}) + racine ({src_p2}).")

        # Identité de la SOURCE : lue UNIQUEMENT pour retrouver ses traces dans
        # le fstab/boot du clone et les remplacer par la nouvelle identité. On
        # ne la recopie jamais sur le clone (voir _rewrite_clone_identity).
        old_ids = {
            "uuid_p1": blkid_value(src_p1, "UUID"),
            "uuid_p2": blkid_value(src_p2, "UUID"),
            "puid_p1": blkid_value(src_p1, "PARTUUID"),
            "puid_p2": blkid_value(src_p2, "PARTUUID"),
            "label_id": disk_label_id(src_disk),
        }

        # Garde-fou taille : la destination doit au moins contenir la BOOT
        # entière + une racine minimale (le contrôle fin vient après montage).
        dst_sectors = int(run(["blockdev", "--getsz", dst_disk]).strip())
        if dst_sectors < p2_start + 262144:      # ~128 Mo mini pour la racine
            raise RuntimeError(f"Destination trop petite : {dst_sectors} secteurs, "
                               f"il en faut au moins {p2_start + 262144}.")

        # NOUVELLE identité disque : le clone reçoit un label-id (donc des
        # PARTUUID) DISTINCT de la source. Source et clone aux mêmes
        # UUID/PARTUUID, branchés ensemble, feraient monter au noyau/systemd la
        # mauvaise partition -- identité unique = plus d'ambiguïté.
        r.step("Table de partitions — identité neuve, racine étendue")
        r.progress(5, "Table de partitions…")
        label_type = "gpt" if "label: gpt" in sfdisk_out else "dos"
        new_label_id = gen_label_id(label_type)
        r.info(f"Nouveau label-id : {new_label_id} (types de partitions "
               "conservés, dernière partition étendue à la cible).")
        script = build_dst_script(sfdisk_out, new_label_id=new_label_id)
        rr = subprocess.run(["sfdisk", "--wipe", "always", dst_disk],
                            input=script, text=True, capture_output=True)
        if rr.returncode != 0:
            raise RuntimeError(f"sfdisk a échoué:\n{rr.stderr}")
        self._wait_for_node(dst_p1)
        self._wait_for_node(dst_p2)

        # Zone bootloader brute (idbloader/u-boot) : APRÈS l'écriture de la
        # table de partitions (elle vit hors partition, le dd ne touche ni le
        # secteur 0 ni les partitions), AVANT les mkfs pour échouer tôt. Ces
        # blobs ne portent aucun UUID de fs : les copier ne recrée pas de
        # collision d'identité.
        self._copy_bootloader_gap(src_disk, dst_disk, p1_start)

        r.step("Formatage — mêmes systèmes de fichiers, UUID neufs")
        r.progress(8, "Formatage…")
        fstype_boot = self._mkfs_like_source(src_p1, dst_p1, "BOOT (p1)")
        fstype_root = self._mkfs_like_source(src_p2, dst_p2, "racine (p2)")

        # Identité FRAÎCHE du clone (posée par mkfs + sfdisk), lue via blkid,
        # puis table de correspondance ancienne -> nouvelle pour la réécriture
        # de fstab/boot après la copie.
        r.step("Montages et correspondance d'identité (source -> clone)")
        r.progress(11, "Montages…")
        new_ids = {
            "uuid_p1": blkid_value(dst_p1, "UUID"),
            "uuid_p2": blkid_value(dst_p2, "UUID"),
            "puid_p1": blkid_value(dst_p1, "PARTUUID"),
            "puid_p2": blkid_value(dst_p2, "PARTUUID"),
            "label_id": disk_label_id(dst_disk),
        }
        self._id_subs = {}
        for key in ("uuid_p1", "uuid_p2", "puid_p1", "puid_p2", "label_id"):
            old, new = old_ids[key], new_ids[key]
            if old and new and old.lower() != new.lower():
                self._id_subs[old] = new
        if self._id_subs:
            r.info("Correspondance d'identité (chaque référence à la source "
                   "sera réécrite vers le clone) :")
            for old, new in sorted(self._id_subs.items(), key=lambda kv: -len(kv[0])):
                r.detail(f"{old}  ->  {new}")
        else:
            r.warn("Identité source illisible : fstab/boot ne seront pas "
                   "réécrits, le clone pourrait ne pas trouver sa racine.")

        # Montage read-only de la source (instantané figé) + montages destination.
        src_boot = self._source_access(src_p1, "/mnt/clone_src_boot")
        src_root = self._source_access(src_p2, "/mnt/clone_src_root")
        self._mount(dst_p1, "/mnt/clone_dst_boot", fstype=fstype_boot)
        self._mount(dst_p2, "/mnt/clone_dst_root", fstype=fstype_root)

        # Divergence UUID : le boot.scr de la source peut désigner la racine par
        # un identifiant ABSENT de blkid (config flash-kernel périmée du
        # master). On le repère et on le mappe vers l'identité NEUVE du clone,
        # sinon la réécriture le raterait et le clone chercherait une racine
        # fantôme.
        self._augment_subs_from_bootscr(src_boot, src_root, new_ids)

        # Garde-fou : l'espace utilisé de la racine source doit tenir dans la
        # partition destination.
        st_src = os.statvfs(src_root)
        used = (st_src.f_blocks - st_src.f_bfree) * st_src.f_frsize
        st_dst = os.statvfs("/mnt/clone_dst_root")
        capacity = st_dst.f_blocks * st_dst.f_frsize
        if used * 1.05 > capacity:
            raise RuntimeError(
                f"Destination trop petite : {used / 1e9:.1f} Go utilisés sur la "
                f"racine source pour {capacity / 1e9:.1f} Go disponibles.")

        r.step("Copie des données (BOOT puis racine)")
        self._start_progress_poll([src_boot, src_root],
                                  ["/mnt/clone_dst_boot", "/mnt/clone_dst_root"])
        r.info("Copie de BOOT…")
        self._rsync(f"{src_boot}/", "/mnt/clone_dst_boot/", dst_fstype=fstype_boot)
        r.info(f"Copie de la racine ({used / 1e9:.1f} Go utilisés — l'étape "
               "longue)…")
        self._rsync(
            f"{src_root}/", "/mnt/clone_dst_root/",
            extra_excludes=["/proc/*", "/sys/*", "/dev/*", "/tmp/*", "/run/*",
                            "/mnt/*", "/media/*", "/lost+found", "/var/tmp/*",
                            "/var/swap"],
        )
        self._stop_progress_poll()
        r.ok("Copie terminée.")

        # Recrée les points de montage exclus, avec les BONS droits (un /tmp
        # sans le sticky bit 1777 casse le boot de plein de services).
        perms = {"proc": 0o555, "sys": 0o555, "dev": 0o755, "tmp": 0o1777,
                 "run": 0o755, "mnt": 0o755, "media": 0o755}
        for sub, mode in perms.items():
            p = os.path.join("/mnt/clone_dst_root", sub)
            os.makedirs(p, exist_ok=True)
            os.chmod(p, mode)

        # Réécrit fstab + configs de régénération + boot du clone pour qu'ils
        # pointent vers SA propre identité. Fait AVANT l'initramfs : le hook
        # flash-kernel du chroot lira ces configs pour régénérer boot.scr.
        r.step("Réécriture de l'identité (fstab, boot, config flash-kernel)")
        r.progress(90, "Réécriture de l'identité…")
        changed = self._rewrite_clone_identity(
            self._id_subs, "/mnt/clone_dst_root", "/mnt/clone_dst_boot")
        self._log_rewrites(changed, "1ʳᵉ passe")
        # Copie connue-bonne du boot.scr patché : filet contre une
        # régénération flash-kernel défectueuse à l'étape suivante.
        self._snapshot_bootscr("/mnt/clone_dst_root", "/mnt/clone_dst_boot")

        # Reconstruit l'initramfs du clone pour qu'il embarque les pilotes du
        # disque cible (NVMe/PCIe, SATA...) — sinon un initramfs hérité d'un
        # système qui bootait ailleurs (SD/USB) ne voit pas le NVMe au démarrage
        # (« Gave up waiting for root device »).
        r.step("Initramfs du clone (chroot ARM64)")
        r.progress(93, "Initramfs (chroot)…")
        self._rebuild_initramfs("/mnt/clone_dst_root", "/mnt/clone_dst_boot")

        r.step("Contrôle final — le clone doit pouvoir booter")
        r.progress(97, "Audit de boot…")
        audit_go = self._finalize_boot("/mnt/clone_dst_root",
                                       "/mnt/clone_dst_boot", [dst_p1, dst_p2])

        # Support de boot séparé (option, LEGACY) : cible qui ne boote pas seule
        # (SSD USB-SATA en pont UAS). On y copie le boot FINAL du clone (kernel +
        # initrd reconstruit + boot.scr validé) ; u-boot lira ce support, la
        # racine restera sur la cible.
        if self.boot_medium:
            self._prepare_boot_medium(self.boot_medium, "/mnt/clone_dst_boot")

        r.info("Synchronisation des écritures (sync)…")
        r.progress(99, "Synchronisation…")
        for mp in ("/mnt/clone_dst_boot", "/mnt/clone_dst_root"):
            subprocess.run(["umount", mp], capture_output=True)
            if mp in self._cleanup_mounts:
                self._cleanup_mounts.remove(mp)
        subprocess.run(["sync"], capture_output=True)

        # -p : sonde directement les périphériques, sans le cache blkid (qui
        # pourrait encore montrer d'anciennes valeurs après re-formatage).
        r.info("Identités finales (source vs clone — elles doivent DIFFÉRER) :")
        r.detail(run(["blkid", "-p", src_p1, src_p2, dst_p1, dst_p2]).strip())
        r.progress(100, "Terminé")
        return audit_go

    # ---------- Restauration d'un bundle partclone ----------
    def _partclone_restore(self, pc, dstp):
        """Restaure une image partclone `pc` dans la partition `dstp`."""
        self._force_unmount(dstp)
        self.r.cmd(f"partclone.restore -s {os.path.basename(pc)} -o {dstp}")
        rr = subprocess.run(["partclone.restore", "-s", pc, "-o", dstp],
                            capture_output=True, text=True)
        if rr.returncode != 0:
            raise RuntimeError(f"partclone.restore a échoué ({dstp}) :\n"
                               f"{(rr.stderr or rr.stdout)[-2000:]}")

    def _do_restore_bundle(self, bundle, dst_disk):
        r = self.r
        r.step("Garde-fous et lecture du bundle partclone")
        r.progress(2, "Préparation…")

        parts = bundle["parts"]
        nums = [n for n, _ in parts]
        if nums != [1, 2]:
            raise RuntimeError(f"Bundle inattendu : partitions {nums} "
                               "(2 attendues : BOOT + rootfs).")
        with open(bundle["sfdisk"], encoding="utf-8") as f:
            src_dump = f.read()
        r.info(f"Bundle : {os.path.basename(bundle['sfdisk'])} + "
               f"{len(parts)} image(s) partclone.")

        for part in disk_partitions(dst_disk):
            self._force_unmount(part)

        # Table de partitions NEUVE : label-id régénéré (identité distincte de la
        # source), dernière partition étendue à la taille de la cible.
        r.step("Table de partitions — identité neuve, racine étendue")
        r.progress(5, "Table de partitions…")
        label_type = "gpt" if "label: gpt" in src_dump else "dos"
        old_label_id = sfdisk_label_id(src_dump)
        new_label_id = gen_label_id(label_type)
        r.info(f"Nouveau label-id : {new_label_id}.")
        script = build_dst_script(src_dump, new_label_id=new_label_id)
        rr = subprocess.run(["sfdisk", "--wipe", "always", dst_disk],
                            input=script, text=True, capture_output=True)
        if rr.returncode != 0:
            raise RuntimeError(f"sfdisk a échoué:\n{rr.stderr}")
        dst_p1, dst_p2 = part_name(dst_disk, 1), part_name(dst_disk, 2)
        self._wait_for_node(dst_p1)
        self._wait_for_node(dst_p2)

        # Restauration partclone (les fs restaurés portent les UUID de la SOURCE).
        r.step("Restauration des partitions (partclone)")
        for i, (num, pc) in enumerate(parts):
            r.progress(10 + i * 30, f"Restauration partition {num}…")
            self._partclone_restore(pc, part_name(dst_disk, num))
        r.ok("Partitions restaurées.")

        # Identité de la SOURCE (à retrouver dans fstab/boot du clone et à
        # remplacer) : fs UUID = ce que partclone vient d'écrire ; PARTUUID/label
        # = dérivés de l'ancien label-id de la table source.
        old_uuid = {n: blkid_value(part_name(dst_disk, n), "UUID") for n in nums}

        # Identité FRAÎCHE : e2fsck (fs propre), resize2fs (rootfs -> remplit la
        # cible), tune2fs -U random (UUID neuf, sinon = celui de la source).
        r.step("Systèmes de fichiers : vérification, extension, UUID neufs")
        for num in nums:
            dstp = part_name(dst_disk, num)
            r.progress(72, "Vérification / redimensionnement…")
            subprocess.run(["e2fsck", "-fy", dstp], capture_output=True, text=True)
            if num == nums[-1]:
                rr = subprocess.run(["resize2fs", dstp], capture_output=True, text=True)
                if rr.returncode != 0:
                    raise RuntimeError(f"resize2fs a échoué ({dstp}):\n{rr.stderr}")
                r.info(f"Racine {dstp} étendue à la taille de la partition.")
            rr = subprocess.run(["tune2fs", "-U", "random", dstp],
                                capture_output=True, text=True)
            if rr.returncode != 0:
                raise RuntimeError(f"tune2fs -U a échoué ({dstp}):\n{rr.stderr}")

        # Table de correspondance ancienne -> nouvelle identité.
        r.step("Correspondance et réécriture de l'identité")
        r.progress(85, "Réécriture de l'identité…")
        self._id_subs = {}
        for num in nums:
            dstp = part_name(dst_disk, num)
            o_uuid, n_uuid = old_uuid[num], blkid_value(dstp, "UUID")
            if o_uuid and n_uuid and o_uuid.lower() != n_uuid.lower():
                self._id_subs[o_uuid] = n_uuid
            n_puid = blkid_value(dstp, "PARTUUID")
            o_puid = (dos_partuuid(old_label_id, num)
                      if label_type == "dos" and old_label_id else "")
            if o_puid and n_puid and o_puid.lower() != n_puid.lower():
                self._id_subs[o_puid] = n_puid
        new_disk_id = disk_label_id(dst_disk)
        if old_label_id:
            o_lab = old_label_id[2:] if old_label_id.lower().startswith("0x") else old_label_id
            if o_lab and new_disk_id and o_lab.lower() != new_disk_id.lower():
                self._id_subs[o_lab] = new_disk_id
        if self._id_subs:
            r.info("Correspondance d'identité (source -> clone) :")
            for old, new in sorted(self._id_subs.items(), key=lambda kv: -len(kv[0])):
                r.detail(f"{old}  ->  {new}")
        else:
            r.warn("Aucune correspondance d'identité construite (UUID illisibles ?).")

        # Montage + réécriture fstab/boot.scr (machinerie commune au clonage).
        self._mount(dst_p1, "/mnt/clone_dst_boot")
        self._mount(dst_p2, "/mnt/clone_dst_root")
        new_ids = {"uuid_p2": blkid_value(dst_p2, "UUID"),
                   "puid_p2": blkid_value(dst_p2, "PARTUUID")}
        self._augment_subs_from_bootscr("/mnt/clone_dst_boot",
                                        "/mnt/clone_dst_root", new_ids)
        changed = self._rewrite_clone_identity(
            self._id_subs, "/mnt/clone_dst_root", "/mnt/clone_dst_boot")
        self._log_rewrites(changed, "1ʳᵉ passe")
        self._snapshot_bootscr("/mnt/clone_dst_root", "/mnt/clone_dst_boot")

        r.step("Initramfs du clone (chroot ARM64)")
        r.progress(92, "Initramfs (chroot)…")
        self._rebuild_initramfs("/mnt/clone_dst_root", "/mnt/clone_dst_boot")

        r.step("Contrôle final — le clone doit pouvoir booter")
        r.progress(97, "Audit de boot…")
        audit_go = self._finalize_boot("/mnt/clone_dst_root",
                                       "/mnt/clone_dst_boot", [dst_p1, dst_p2])

        r.info("Synchronisation des écritures (sync)…")
        for mp in ("/mnt/clone_dst_boot", "/mnt/clone_dst_root"):
            subprocess.run(["umount", mp], capture_output=True)
            if mp in self._cleanup_mounts:
                self._cleanup_mounts.remove(mp)
        subprocess.run(["sync"], capture_output=True)

        r.info("Identités finales du clone :")
        r.detail(run(["blkid", "-p", dst_p1, dst_p2]).strip())
        r.progress(100, "Terminé")
        return audit_go
