#!/usr/bin/env python3
"""
clone_engine.py — MOTEUR du clonage de disque Odroid (sans interface).

Toute la mécanique à état du clonage (montages, rsync, mkfs, réécriture
d'identité, initramfs, progression) vit ici, derrière deux callbacks (`log` et
`progress`) : l'onglet GUI (`clone_panel.ClonePanel`) et les sous-commandes CLI
(`station.py clone` / `station.py image`) pilotent LE MÊME moteur — pas de
duplication, mêmes garde-fous des deux côtés. La logique pure sans état (blkid,
sfdisk, patch boot.scr…) reste dans `clone_core.py` (testée sans matériel).

Voir le docstring de `clone_panel.py` pour le POURQUOI de chaque étape
(clone à froid, mode de boot SPI vs disque, identité neuve, initramfs…) — ce
fichier n'en est que le COMMENT.
"""

import glob
import os
import platform
import re
import stat
import subprocess
import threading
import time

from clone_core import (
    BOOTLOADER_FIRST_SECTOR, blkid_value, bootloader_gap_present,
    build_dst_script, disk_label_id, extract_root_ids, fs_used, gen_label_id,
    image_size_bytes, is_system_mp, parse_start_size, part_name,
    rewrite_uboot_script, run, storage_modules,
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


class CloneEngine:
    """Un clonage = une instance. `log` reçoit chaque ligne de journal,
    `progress` (optionnel) reçoit (pourcentage, texte court). `boot_mode` est
    « spi » (défaut, flotte) ou « disk » (legacy eMMC/SD auto-bootable) ;
    `boot_medium` est le disque du support de boot séparé legacy, ou None.

    Le clonage est TOUJOURS à froid : le disque système en marche est refusé en
    source comme en destination (`assert_not_system_disk`, garde-fou absolu).

    Deux points d'entrée : `clone()` (disque ou image -> disque) et
    `make_image()` (disque -> fichier image COMPACT, futur `--image` de clone()).
    Chacun gère le losetup éventuel, le nettoyage (démontages, détachement loop)
    même en cas d'erreur, LÈVE en cas d'échec, et retourne le message de fin.
    """

    def __init__(self, log, progress=None, boot_mode="spi", boot_medium=None):
        if boot_mode not in ("spi", "disk"):
            raise ValueError(f"boot_mode inconnu : {boot_mode!r} (attendu 'spi' ou 'disk')")
        self.log = log
        self._progress = progress if progress is not None else (lambda pct, text: None)
        self.boot_mode = boot_mode
        self.boot_medium = boot_medium
        self.loop_dev = None
        self._cleanup_mounts = []     # points de montage créés PAR NOUS (cleanup)
        self._poll_stop = None
        self._id_subs = {}

    # ---------- Point d'entrée ----------
    def clone(self, src_disk, dst_disk, img_path=None):
        try:
            if img_path:
                self.log(f"Attachement de l'image {img_path} en loop device...")
                loop = run(["losetup", "--find", "--show", "-P", img_path]).strip()
                self.loop_dev = loop
                src_disk = loop
                self._wait_for_node(part_name(loop, 1))
                self.log(f"Image montée sur {loop}")

            self._do_clone(src_disk, dst_disk)
            self.log("\n=== TERMINÉ AVEC SUCCÈS ===")
            self.log("Le clone a sa PROPRE identité (UUID/PARTUUID neufs, fstab, "
                     "config bootloader + boot.scr réécrits, initramfs "
                     "reconstruit) : aucune collision d'identité avec la source "
                     "si les deux disques restent branchés ensemble.")
            if self.boot_medium:
                return (f"Clonage terminé.\n\nRacine sur {dst_disk}, boot préparé sur "
                        f"{self.boot_medium}. Démarre l'ODROID avec le support de boot "
                        f"({self.boot_medium}) + la cible ({dst_disk}) branchés ; "
                        "retire la source.")
            if self.boot_mode == "spi":
                return ("Clonage terminé avec succès.\n\nLe clone possède ses propres "
                        "UUID/PARTUUID (identité régénérée, fstab + boot.scr + "
                        "initramfs mis à jour).\n\nMode SPI : le clone ne boote PAS "
                        "seul depuis le disque — il faut une carte ODROID-M1 dont la "
                        "PUCE SPI porte le golden (u-boot Armbian). Sur une telle "
                        "carte, u-boot cible le NVMe en premier et boote le clone. "
                        "Flashe la SPI (onglet SPI, ou odroid-station spi "
                        "flash), puis vérifie avec odroid-station check.")
            return ("Clonage terminé avec succès.\n\nLe clone possède ses propres "
                    "UUID/PARTUUID (identité régénérée, fstab + boot + initramfs "
                    "mis à jour). Mode disque : pour démarrer sur le clone, règle "
                    "l'ordre de boot (ou retire la source / la SD).")
        finally:
            self._cleanup()

    def make_image(self, src_disk, img_path, margin=1.25):
        """Crée une IMAGE DISQUE COMPACTE de `src_disk` dans le fichier
        `img_path`, utilisable ensuite comme source de clonage (GUI « Fichier
        image » / CLI `--image`).

        Même pipeline que le clonage disque -> disque (`_do_clone` : identité
        neuve, fstab/boot.scr réécrits, initramfs), mais la destination est un
        fichier attaché en loop device et DIMENSIONNÉ sur l'espace UTILISÉ de la
        racine source, pas sur la capacité du disque (`image_size_bytes`) : un
        NVMe 128 Go rempli à 20 % donne une image ~30 Go, pas 128. Deux
        mécanismes se cumulent :
          - géométrie : la racine de l'image remplit un fichier taillé au plus
            juste, et sera RÉ-ÉTENDUE à la taille de la vraie cible au clonage
            depuis l'image (le size= de la dernière partition saute des deux
            côtés) ;
          - fichier SPARSE (truncate) : seuls les octets réellement écrits
            occupent le disque hôte.
        En cas d'échec, le fichier partiel est SUPPRIMÉ (une image tronquée qui
        traîne finirait par servir de source de clonage).
        """
        created = False
        try:
            p2_start, root_used = self._measure_for_image(src_disk)
            size = image_size_bytes(p2_start, root_used, margin=margin)
            self.log(f"Racine source : {root_used / 1e9:.1f} Go utilisés -> "
                     f"image compacte de {size / 1e9:.1f} Go (marge métadonnées "
                     "incluse), fichier sparse.")
            with open(img_path, "wb") as f:
                f.truncate(size)               # sparse : aucun octet écrit
            created = True
            loop = run(["losetup", "--find", "--show", "-P", img_path]).strip()
            self.loop_dev = loop
            self.log(f"Image attachée en loop device : {loop}")

            self._do_clone(src_disk, loop)

            # Détache AVANT la mesure finale : c'est le détachement qui garantit
            # que tout le cache du loop est retombé dans le fichier.
            run(["losetup", "-d", loop], check=False)
            self.loop_dev = None
            st = os.stat(img_path)
            real = st.st_blocks * 512
            self.log(f"\n=== IMAGE CRÉÉE ===\n{img_path}\n"
                     f"Taille logique : {st.st_size / 1e9:.1f} Go — occupé réel "
                     f"(sparse) : {real / 1e9:.1f} Go.")
            return (f"Image compacte créée : {img_path}\n\n"
                    f"Taille logique {st.st_size / 1e9:.1f} Go, occupé réel "
                    f"{real / 1e9:.1f} Go (fichier sparse : préserver les trous "
                    "avec `cp --sparse=always` / `rsync -S`, ou compresser en "
                    ".zst pour la distribuer).\n\n"
                    "Utilisable comme SOURCE de clonage : onglet Clone -> "
                    "« Fichier image », ou odroid-station clone --image … --dest "
                    "/dev/nvme0n1 (la racine sera ré-étendue à la taille de la "
                    "cible).")
        except BaseException:
            if created:
                self._cleanup()          # détache le loop AVANT de supprimer
                try:
                    os.remove(img_path)
                    self.log(f"Image partielle supprimée : {img_path}")
                except OSError:
                    pass
            raise
        finally:
            self._cleanup()

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

        # Source = montage read-only figé (clone à froid) : aucune tolérance aux
        # transferts partiels, on exige le code 0.
        ok_codes = (0,)
        last = None
        for flags in attempts:
            r = subprocess.run(["rsync", *flags, *excludes, src, dst],
                               capture_output=True, text=True)
            if r.returncode in ok_codes:
                if r.returncode == 24:
                    self.log("rsync : des fichiers ont disparu pendant la copie "
                             "(code 24, normal sur un système en marche).")
                return
            last = r
            self.log(f"rsync {' '.join(flags)} a échoué (code {r.returncode}), "
                     "tentative suivante...")
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
        if self.boot_mode == "spi":
            if bootloader_gap_present(self._read_sector64(src_disk)):
                self.log(
                    "Mode SPI : idbloader résiduel au secteur 64 de la source "
                    "(vestige d'un ancien clone disque-boot) — laissé de côté, "
                    "SANS effet : le boot passe par la puce SPI + u-boot. Zone "
                    "bootloader NON recopiée (inutile).")
            else:
                self.log(
                    "Mode SPI : pas de bootloader sur le disque (normal) — le "
                    "boot passe par la puce SPI + u-boot. Zone bootloader ignorée.")
            self.log("→ Le clone ne bootera que sur une carte dont la SPI "
                     "porte le golden (onglet SPI / odroid-station spi flash).")
            return

        count = p1_start - BOOTLOADER_FIRST_SECTOR
        if count <= 0:
            self.log(f"⚠ Partition 1 démarre au secteur {p1_start} "
                     f"(< {BOOTLOADER_FIRST_SECTOR}) : pas de zone "
                     "bootloader à copier. Clone probablement non "
                     "bootable en démarrage direct SD/eMMC.")
            return
        kib = count * 512 // 1024
        taille = f"{kib} Ko" if kib < 1024 else f"{kib / 1024:.1f} Mo"
        self.log(f"Copie du bootloader (secteurs "
                 f"{BOOTLOADER_FIRST_SECTOR} -> {p1_start} : "
                 f"idbloader + u-boot, {taille})...")
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
        self.log("Bootloader copié et vérifié (secteur 64 identique).")

    # ---------- mkfs ----------
    def _mkfs_like_source(self, src_part, dst_part):
        """Formate la destination avec le MÊME type de fs que la source, mais
        avec un UUID FRAIS.

        On ne recopie PAS l'UUID de la source : mkfs génère un UUID neuf,
        unique, si bien que le clone n'entre pas en collision d'identité avec la
        source (deux fs au même UUID branchés ensemble -> le noyau/systemd peut
        monter le mauvais). La cohérence fstab/boot est rétablie ensuite par
        `_rewrite_clone_identity`. Le label (nom de volume) est conservé :
        cosmétique, non utilisé pour désigner la racine sur les images ODROID
        (elles passent par UUID/PARTUUID). Bonus : pas d'UUID dupliqué avec un
        fs déjà monté au moment du mount -> pas de refus « Operation not
        permitted ».
        """
        fstype = blkid_value(src_part, "TYPE")
        label = blkid_value(src_part, "LABEL")
        if fstype == "vfat":
            cmd = ["mkfs.vfat"]
            if label:
                cmd += ["-n", label[:11]]
            cmd.append(dst_part)
        elif fstype in ("ext2", "ext3", "ext4"):
            cmd = [f"mkfs.{fstype}", "-F", "-q"]
            if label:
                cmd += ["-L", label]
            cmd.append(dst_part)
        else:
            raise RuntimeError(f"Type de système de fichiers non géré sur "
                               f"{src_part} : {fstype or 'inconnu'}")
        self.log(f"Formatage {dst_part} ({fstype}, UUID frais)...")
        # udisks2/GNOME peut auto-monter la partition sitôt créée : on démonte
        # juste avant de formater (mkfs refuse un fs monté).
        self._force_unmount(dst_part)
        run(cmd)
        return fstype

    # ---------- Divergence UUID du boot.scr ----------
    def _augment_subs_from_bootscr(self, src_boot, src_root, new_ids):
        """Ajoute à `self._id_subs` tout identifiant que le `boot.scr` de la
        source désigne comme racine mais qui est ABSENT de blkid (divergence
        connue). Chacun est mappé vers l'identité NEUVE du clone du même type
        (UUID -> nouvel UUID rootfs, PARTUUID -> nouveau PARTUUID rootfs), à
        longueur constante (donc patchable par `rewrite_uboot_script`).
        """
        candidates = [os.path.join(src_boot, "boot.scr"),
                      os.path.join(src_root, "boot", "boot.scr")]
        scr = next((p for p in candidates if os.path.isfile(p)), None)
        if not scr:
            return
        try:
            with open(scr, "rb") as f:
                data = f.read()
        except OSError:
            return
        known = {k.lower() for k in self._id_subs}
        for kind, val in extract_root_ids(data):
            if val.lower() in known:
                continue                       # déjà couvert par blkid
            new = new_ids["puid_p2"] if kind == "PARTUUID" else new_ids["uuid_p2"]
            if not new:
                continue
            self._id_subs[val] = new
            known.add(val.lower())
            self.log(
                f"⚠ boot.scr désigne la racine par {kind}={val}, identifiant "
                f"ABSENT de blkid (divergence connue). Mappé vers l'identité "
                f"neuve du clone ({new}). À vérifier sur le master avant un "
                "clonage de flotte (voir docs/DEPLOIEMENT_FLOTTE.md).")

    # ---------- Réécriture de l'identité (fstab + boot) ----------
    def _rewrite_clone_identity(self, subs, root_mp, boot_mp):
        """Remplace, DANS LA CONFIG DU CLONE, chaque identifiant de la source
        par celui du clone (table `subs` : ancien -> nouveau).

        Sans ça, le clone -- qui porte de NOUVEAUX UUID/PARTUUID -- garderait un
        fstab et une config bootloader (extlinux.conf/boot.ini/armbianEnv.txt,
        cmdline...) pointant encore vers les UUID de la SOURCE : le noyau ne
        trouverait pas sa racine, ou monterait la partition de la source. C'est
        la moitié « cohérence » du clone, l'autre étant l'identité neuve
        elle-même.

        Balaie /etc/fstab et tout fichier de la partition BOOT et de <root>/boot.
        Les fichiers TEXTE sont réécrits directement. Les **images script u-boot**
        (`boot.scr`, magic 0x27051956) — binaires mais qui embarquent typiquement
        `root=UUID=…` sur ODROID — sont patchées en place puis leurs CRC refaits
        (voir `rewrite_uboot_script`). Les autres binaires (noyau, dtb, initrd)
        sont ignorés : un remplacement naïf les corromprait.
        """
        if not subs:
            self.log("⚠ Aucune correspondance d'identité à réécrire "
                     "(UUID source illisibles ?).")
            return
        # clés les plus longues d'abord : un PARTUUID (label-id + suffixe)
        # doit être remplacé avant le label-id nu qu'il contient.
        ordered = sorted(subs.items(), key=lambda kv: -len(kv[0]))

        targets = [os.path.join(root_mp, "etc", "fstab")]
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

        if changed:
            for p in changed:
                self.log(f"  identité réécrite dans {p}")
        else:
            self.log("⚠ Aucun fichier de config ne référençait l'identité "
                     "de la source (fstab/extlinux/boot.ini/boot.scr...). "
                     "Vérifie manuellement que le clone pointe vers SES UUID.")

    # ---------- Initramfs (voit le disque cible au démarrage) ----------
    def _rebuild_initramfs(self, root_mp, boot_mp):
        """Reconstruit l'initramfs du clone (chroot + update-initramfs) en y
        ajoutant les pilotes du disque cible.

        Nécessite un chroot de MÊME architecture : ARM64 -> il faut tourner sur
        l'ODROID (ou un autre ARM64). Sur un hôte x86, on saute proprement en
        indiquant la commande à lancer plus tard sur l'ODROID.
        """
        if platform.machine() not in ("aarch64", "arm64"):
            self.log(
                f"⚠ Initramfs NON reconstruit (hôte {platform.machine()}, pas "
                "ARM64). À faire sur l'ODROID une fois le clone en place :\n"
                "    sudo update-initramfs -u   (avec les modules NVMe/PCIe)")
            return
        modfile = os.path.join(root_mp, "etc", "initramfs-tools", "modules")
        if not os.path.isfile(modfile):
            self.log("⚠ Pas d'initramfs-tools dans le clone : rien à "
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
            self.log(f"⚠ Impossible d'écrire {modfile} : {e}")
            return

        self.log(f"Reconstruction de l'initramfs (modules cible : "
                 f"{', '.join(mods) or 'aucun'})...")
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
            r = subprocess.run(["chroot", root_mp, "update-initramfs", "-u", "-k", "all"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                self.log("⚠ update-initramfs a échoué (le clone risque de ne "
                         "pas voir sa racine au boot) :\n" + (r.stderr or r.stdout)[-1500:])
            else:
                self.log("Initramfs reconstruit : le clone verra son disque "
                         "cible au démarrage.")
        except Exception as e:
            self.log(f"⚠ Reconstruction initramfs impossible : {e}")
        finally:
            for dst in reversed(binds):
                subprocess.run(["umount", dst], capture_output=True)

    # ---------- Support de boot séparé (USB/SD) ----------
    def _prepare_boot_medium(self, boot_disk, dst_boot_mp):
        """Efface `boot_disk` et y écrit une partition de boot unique, copie du
        boot du clone (kernel + initrd reconstruit + boot.scr déjà réglé sur
        l'UUID de la cible). u-boot lit ce support (USB/SD), la racine reste sur
        la cible — indispensable quand la cible ne boote pas seule (SSD USB-SATA
        en pont UAS qu'u-boot ne pilote pas ; un NVMe boote seul via la SPI).
        """
        assert_not_system_disk(boot_disk, role="support de boot")
        self.log(f"Préparation du support de boot {boot_disk} (EFFACÉ)...")
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
        self.log(f"Support de boot prêt : démarre l'ODROID sur {boot_disk} "
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
                self._progress(pct, f"Copie... {pct:.0f} %")
                stop.wait(0.5)

        threading.Thread(target=loop, daemon=True).start()

    def _stop_progress_poll(self):
        if self._poll_stop is not None:
            self._poll_stop.set()
            self._poll_stop = None

    # ---------- Cœur du clonage ----------
    def _do_clone(self, src_disk, dst_disk):
        log = self.log
        self._progress(0, "Préparation...")

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

        log("Lecture de la géométrie source...")
        sfdisk_out = run(["sfdisk", "-d", src_disk])
        p1_start, p1_size = parse_start_size(sfdisk_out, src_p1)
        p2_start, _ = parse_start_size(sfdisk_out, src_p2)

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
        label_type = "gpt" if "label: gpt" in sfdisk_out else "dos"
        new_label_id = gen_label_id(label_type)
        log(f"Création de la table de partitions sur {dst_disk} "
            f"(NOUVELLE identité label-id={new_label_id}, types conservés, "
            "racine étendue)...")
        script = build_dst_script(sfdisk_out, new_label_id=new_label_id)
        r = subprocess.run(["sfdisk", "--wipe", "always", dst_disk],
                           input=script, text=True, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"sfdisk a échoué:\n{r.stderr}")
        self._wait_for_node(dst_p1)
        self._wait_for_node(dst_p2)

        # Zone bootloader brute (idbloader/u-boot) : APRÈS l'écriture de la
        # table de partitions (elle vit hors partition, le dd ne touche ni le
        # secteur 0 ni les partitions), AVANT les mkfs pour échouer tôt. Ces
        # blobs ne portent aucun UUID de fs : les copier ne recrée pas de
        # collision d'identité.
        self._copy_bootloader_gap(src_disk, dst_disk, p1_start)

        fstype_boot = self._mkfs_like_source(src_p1, dst_p1)
        fstype_root = self._mkfs_like_source(src_p2, dst_p2)

        # Identité FRAÎCHE du clone (posée par mkfs + sfdisk), lue via blkid,
        # puis table de correspondance ancienne -> nouvelle pour la réécriture
        # de fstab/boot après la copie.
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
            log("Correspondance d'identité (source -> clone) :")
            for old, new in sorted(self._id_subs.items(), key=lambda kv: -len(kv[0])):
                log(f"  {old} -> {new}")
        else:
            log("⚠ Identité source illisible : fstab/boot ne seront pas "
                "réécrits, le clone pourrait ne pas trouver sa racine.")

        # Montage read-only de la source (instantané figé) + montages destination.
        src_boot = self._source_access(src_p1, "/mnt/clone_src_boot")
        src_root = self._source_access(src_p2, "/mnt/clone_src_root")
        self._mount(dst_p1, "/mnt/clone_dst_boot", fstype=fstype_boot)
        self._mount(dst_p2, "/mnt/clone_dst_root", fstype=fstype_root)

        # Divergence UUID : le boot.scr de la source peut désigner la racine par
        # un identifiant ABSENT de blkid (constaté sur ce projet :
        # root=UUID=eee2b90d… alors que nvme0n1p2 porte a9bdb4f9…). On le repère
        # dans le boot.scr et on le mappe vers l'identité NEUVE du clone, sinon la
        # réécriture le raterait et le clone chercherait une racine fantôme.
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

        self._start_progress_poll([src_boot, src_root],
                                  ["/mnt/clone_dst_boot", "/mnt/clone_dst_root"])

        log("Copie de BOOT...")
        self._rsync(f"{src_boot}/", "/mnt/clone_dst_boot/", dst_fstype=fstype_boot)

        log("Copie de la racine (peut prendre du temps)...")
        self._rsync(
            f"{src_root}/", "/mnt/clone_dst_root/",
            extra_excludes=["/proc/*", "/sys/*", "/dev/*", "/tmp/*", "/run/*",
                            "/mnt/*", "/media/*", "/lost+found", "/var/tmp/*",
                            "/var/swap"],
        )

        # Recrée les points de montage exclus, avec les BONS droits (un /tmp
        # sans le sticky bit 1777 casse le boot de plein de services).
        perms = {"proc": 0o555, "sys": 0o555, "dev": 0o755, "tmp": 0o1777,
                 "run": 0o755, "mnt": 0o755, "media": 0o755}
        for sub, mode in perms.items():
            p = os.path.join("/mnt/clone_dst_root", sub)
            os.makedirs(p, exist_ok=True)
            os.chmod(p, mode)

        # Réécrit fstab + config bootloader du clone pour qu'ils pointent vers
        # SA propre identité (et non celle de la source). Fait AVANT le
        # démontage, tant que les partitions du clone sont accessibles.
        log("Réécriture de l'identité dans la config du clone (fstab, boot)...")
        self._rewrite_clone_identity(self._id_subs, "/mnt/clone_dst_root",
                                     "/mnt/clone_dst_boot")

        self._stop_progress_poll()
        self._progress(99, "Initramfs + finalisation...")

        # Reconstruit l'initramfs du clone pour qu'il embarque les pilotes du
        # disque cible (NVMe/PCIe, SATA...) — sinon un initramfs hérité d'un
        # système qui bootait ailleurs (SD/USB) ne voit pas le NVMe au démarrage
        # (« Gave up waiting for root device »).
        self._rebuild_initramfs("/mnt/clone_dst_root", "/mnt/clone_dst_boot")

        # Support de boot séparé (option, LEGACY) : cible qui ne boote pas seule
        # (SSD USB-SATA en pont UAS). On y copie le boot du clone (kernel +
        # initrd reconstruit + boot.scr déjà réglé sur l'UUID cible) ; u-boot
        # lira ce support, la racine restera sur la cible.
        if self.boot_medium:
            self._prepare_boot_medium(self.boot_medium, "/mnt/clone_dst_boot")

        log("Synchronisation des écritures (sync)...")
        for mp in ("/mnt/clone_dst_boot", "/mnt/clone_dst_root"):
            subprocess.run(["umount", mp], capture_output=True)
            if mp in self._cleanup_mounts:
                self._cleanup_mounts.remove(mp)
        subprocess.run(["sync"], capture_output=True)

        log("\nVérification finale (le clone doit avoir des UUID/PARTUUID "
            "DIFFÉRENTS de la source) :")
        # -p : sonde directement les périphériques, sans le cache blkid (qui
        # pourrait encore montrer d'anciennes valeurs après re-formatage).
        log(run(["blkid", "-p", src_p1, src_p2, dst_p1, dst_p2]))
