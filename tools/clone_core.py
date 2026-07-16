#!/usr/bin/env python3
"""
clone_core.py — logique PURE (sans interface) du clonage de disque Odroid.

Séparé de `clone_panel.py` (l'interface tkinter) pour garder chaque fichier
raisonnable et rendre cette logique testable sans GUI ni matériel
(`tests/test_clone_core.py`, `tests/test_clone_uboot.py`). Fonctions autonomes :
lecture blkid/lsblk, nommage de partitions, génération d'identité, réécriture du
script sfdisk, patch d'image u-boot, détection de modules, etc. La mécanique à
état (montages, rsync, chroot, progression) reste dans la classe GUI.
"""

import json
import os
import re
import secrets
import struct
import subprocess
import uuid
import zlib


def run(cmd, check=True):
    """Exécute une commande et retourne stdout."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Commande échouée: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout


def blkid_value(dev, tag):
    """Valeur d'un tag blkid (TYPE, UUID, PARTUUID, LABEL, PTUUID...) ; ''
    si absent.

    `-c /dev/null` : on court-circuite le cache blkid. Sur un disque qu'on
    vient de repartitionner/reformater, le cache peut encore contenir l'ANCIEN
    UUID -> la table de correspondance ancienne->nouvelle serait fausse et la
    réécriture fstab/boot inopérante. On sonde donc toujours le périphérique.
    """
    r = subprocess.run(["blkid", "-c", "/dev/null", "-s", tag, "-o", "value", dev],
                       capture_output=True, text=True)
    return r.stdout.strip()


def list_block_devices():
    """Liste des disques physiques (pas les partitions), avec repérage du
    disque qui porte le système en cours d'exécution, le bus (usb/nvme/sata…)
    et le numéro de série — de quoi identifier un disque SANS ambiguïté dans
    l'interface (deux clés USB de même modèle se distinguent par leur série)."""
    out = run(["lsblk", "-J", "-o", "NAME,SIZE,MODEL,TYPE,MOUNTPOINT,TRAN,SERIAL"])
    data = json.loads(out)

    def mountpoints(node):
        mps = [node.get("mountpoint")] if node.get("mountpoint") else []
        for child in node.get("children", []) or []:
            mps.extend(mountpoints(child))
        return mps

    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        is_system = "/" in mountpoints(dev)
        disks.append({
            "path": f"/dev/{dev['name']}",
            "size": dev.get("size", "?"),
            "model": (dev.get("model") or "").strip(),
            "tran": (dev.get("tran") or "").strip(),
            "serial": (dev.get("serial") or "").strip(),
            "system": is_system,
        })
    return disks


def disk_display_label(d):
    """Libellé HUMAIN d'un disque pour les listes de l'interface :
    « /dev/sda · 119,2G · Samsung SSD 870 · usb · S/N ABC123 [SYSTÈME] »."""
    parts = [d["path"], d.get("size", "?")]
    if d.get("model"):
        parts.append(d["model"])
    if d.get("tran"):
        parts.append(d["tran"])
    if d.get("serial"):
        parts.append(f"S/N {d['serial']}")
    label = "  ·  ".join(parts)
    if d.get("system"):
        label += "   [DISQUE SYSTÈME]"
    return label


def part_name(disk, num):
    """Gère /dev/sdX1 vs /dev/nvme0n1p1 / /dev/mmcblk0p1 / /dev/loop0p1."""
    if disk[-1].isdigit():
        return f"{disk}p{num}"
    return f"{disk}{num}"


def disk_label_id(disk):
    """Identifiant de la table de partitions (label-id / PTUUID), lu via
    `blkid` -- PTUUID pour un disque entier, pas une partition.

    Sert ici en LECTURE des deux côtés : sur la source pour retrouver ses
    PARTUUID dans la config du clone (et les remplacer), sur la destination
    pour connaître la NOUVELLE identité effectivement écrite. On ne recopie
    jamais le label-id de la source sur le clone : identité distincte
    obligatoire pour éviter les collisions UUID/PARTUUID (voir _do_clone).
    """
    return blkid_value(disk, "PTUUID")


def gen_label_id(label_type):
    """Génère un label-id disque NEUF, distinct de la source.

    - dos (MBR) : 32 bits -> 8 chiffres hex (préfixe 0x posé plus tard) ;
    - gpt : UUID aléatoire.
    Une identité propre évite toute collision UUID/PARTUUID entre le clone et la
    source si les deux disques restent branchés ensemble.
    """
    if label_type == "gpt":
        return str(uuid.uuid4())
    return f"{secrets.randbits(32):08x}"


def build_dst_script(src_dump, new_label_id=None):
    """Reconstruit un script sfdisk pour la destination à partir du dump
    (`sfdisk -d`) de la source.

    - conserve `label:` (dos/gpt) et `unit:` ;
    - le `label-id:` de la SOURCE est volontairement IGNORÉ. `new_label_id`,
      si fourni, FORCE une NOUVELLE identité disque sur le clone : clone et
      source ne doivent pas partager le même PARTUUID (sinon le noyau peut monter
      la mauvaise partition quand les deux sont branchés). La cohérence
      fstab/boot est rétablie ensuite par `_rewrite_clone_identity` ;
    - conserve start / type / bootable de chaque partition ;
    - retire `size=` de la DERNIÈRE partition -> elle remplit la destination
      (clé plus grande = racine plus grande, comme le SD Card Copier).
    """
    header, parts, label_type = [], [], "dos"
    for line in src_dump.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("label:"):
            label_type = s.split(":", 1)[1].strip()
            header.append(s)
        elif s.startswith("label-id:"):
            continue   # identité régénérée : on ignore celle de la source
        elif s.startswith("unit:"):
            header.append(s)
        elif "start=" in s and ":" in s:
            parts.append(s.split(":", 1)[1].strip())   # retire "/dev/xxxN :"
    if not parts:
        raise RuntimeError("Aucune partition dans le dump sfdisk de la source.")

    if new_label_id:
        if label_type == "dos" and not new_label_id.lower().startswith("0x"):
            new_label_id = f"0x{new_label_id}"
        insert_at = 1 if header and header[0].startswith("label:") else 0
        header.insert(insert_at, f"label-id: {new_label_id}")

    fields = [f.strip() for f in parts[-1].split(",")]
    parts[-1] = ", ".join(f for f in fields if not f.startswith("size="))
    return "\n".join(header) + "\n\n" + "\n".join(parts) + "\n"


# Premier secteur de la zone bootloader Rockchip : le BootROM du RK3568 charge
# idbloader.img au secteur 64 (0x40) ; u-boot.itb suit au secteur 16384 (0x4000).
# On ne copie PAS les secteurs 0-63 : le secteur 0 porte la table de partitions
# fraîchement écrite (racine étendue) et les secteurs 2-33 porteraient les
# entrées GPT ; rien d'utile au boot n'y vit sur les images ODROID.
BOOTLOADER_FIRST_SECTOR = 64

# Signature de l'en-tête idbloader Rockchip (RK_SIGNATURE = 0x0ff0aa55 dans
# tools/rkcommon.c d'u-boot), écrite en little-endian au tout début du secteur
# 64 sur les images SD/eMMC/NVMe qui bootent depuis le disque.
RK_IDBLOADER_MAGIC = b"\x55\xaa\xf0\x0f"


def bootloader_gap_present(sector64_bytes):
    """Le secteur 64 porte-t-il un idbloader Rockchip ? **PUREMENT INFORMATIF.**

    Sert UNIQUEMENT à repérer un idbloader résiduel (vestige d'un ancien clone
    disque-boot) sur une cible qui, en réalité, boote via la puce SPI. Cette
    détection ne doit **jamais** sélectionner le mode de boot : sur ce projet le
    boot passe par la SPI+u-boot pour toute la flotte, et un vestige au secteur
    64 ne rend pas le disque auto-bootable (cf. clone_panel, `--boot-mode`
    explicite, défaut `spi`). Heuristique volontairement conservatrice : on teste
    le magic RK ; à défaut (secteur nul/aléatoire) on répond « absent ».
    """
    if not sector64_bytes or len(sector64_bytes) < 4:
        return False
    return sector64_bytes[:4] == RK_IDBLOADER_MAGIC


# --------------------------------------------------------------------------
# Sauvegarde « bundle » partclone (table sfdisk + une image .pc par partition)
# --------------------------------------------------------------------------
# Format produit par une sauvegarde type Clonezilla / partclone :
#   <prefixe>-partition-table.sfdisk   table de partitions (sfdisk -d)
#   <prefixe><N>-<label>.pc            image partclone de la partition N
#   <prefixe>-first1M.bin              (optionnel) 1er Mio brut (table + gap)
# Restaurable sur un disque vierge : cf. CloneEngine.restore_bundle.
def find_partclone_bundle(dirpath):
    """Détecte une sauvegarde bundle partclone dans le répertoire `dirpath`.

    Retourne un dict {'sfdisk': chemin, 'first1m': chemin|None,
    'parts': [(num, chemin_pc), ...] trié par numéro de partition}, ou None si
    le répertoire ne contient pas au moins une table `.sfdisk` et une image
    `.pc` nommée `…<N>-….pc`. Ne lit aucun contenu (`partclone.restore`
    validera les images lui-même) : testable sans matériel.
    """
    if not dirpath or not os.path.isdir(dirpath):
        return None
    entries = sorted(os.listdir(dirpath))
    sfdisks = [e for e in entries if e.endswith(".sfdisk")]
    if not sfdisks:
        return None
    parts = []
    for e in entries:
        # numéro = dernier groupe de chiffres juste avant « -<label>.pc »
        # (marche pour sda1-…, mmcblk0p1-…, nvme0n1p2-…).
        m = re.search(r"(\d+)-[^-]*\.pc$", e)
        if m:
            parts.append((int(m.group(1)), os.path.join(dirpath, e)))
    if not parts:
        return None
    parts.sort()
    first1m = next((os.path.join(dirpath, e) for e in entries
                    if e.lower().endswith("first1m.bin")), None)
    return {
        "sfdisk": os.path.join(dirpath, sfdisks[0]),
        "first1m": first1m,
        "parts": parts,
    }


def sfdisk_label_id(dump):
    """`label-id:` d'un dump sfdisk (`0x…` en dos, UUID en gpt), ou None."""
    m = re.search(r"(?mi)^\s*label-id:\s*(\S+)", dump)
    return m.group(1) if m else None


def dos_partuuid(label_id, num):
    """PARTUUID d'une partition MBR tel que le voit blkid : les 8 chiffres hex
    du label-id (sans `0x`, minuscules) + `-NN` (numéro sur 2 chiffres).
    Ex. ('0x9d6cd9bd', 1) -> '9d6cd9bd-01'."""
    h = label_id.lower()
    if h.startswith("0x"):
        h = h[2:]
    return f"{h}-{num:02d}"


def parse_start_size(src_dump, part_dev):
    """(start, size) en secteurs d'une partition dans un dump sfdisk."""
    for line in src_dump.splitlines():
        s = line.strip()
        if s.startswith(part_dev + " ") or s.startswith(part_dev + ":"):
            start = int(s.split("start=")[1].split(",")[0].strip())
            size = int(s.split("size=")[1].split(",")[0].strip())
            return start, size
    raise RuntimeError(f"Partition {part_dev} introuvable dans le dump sfdisk.")


def is_system_mp(mp):
    """Vrai si le point de montage appartient au système en cours."""
    return mp == "/" or mp.startswith(("/boot", "/usr"))


def fs_used(path):
    """Octets utilisés sur le système de fichiers monté à `path` (statvfs)."""
    st = os.statvfs(path)
    return (st.f_blocks - st.f_bfree) * st.f_frsize


# Plancher de la partition racine d'une image compacte : en dessous, la marge
# proportionnelle ne couvre plus les fixes (journal ext4, inodes, divergence
# statvfs/rsync). Reste largement au-dessus du minimum exigé par le moteur
# (p2_start + 262144 secteurs ≈ 128 MiO).
IMG_ROOT_FLOOR_BYTES = 512 * 2**20


def image_size_bytes(p2_start_sectors, root_used_bytes, margin=1.25,
                     floor_bytes=IMG_ROOT_FLOOR_BYTES):
    """Taille totale (octets) d'une IMAGE DISQUE COMPACTE de la source.

    Tout ce qui précède la racine (zone bootloader + partition BOOT, soit
    `p2_start_sectors` secteurs de 512 octets) est conservé à l'identique ; la
    racine, elle, est dimensionnée sur l'espace UTILISÉ de la source (et non la
    capacité du disque) : un NVMe 128 Go rempli à 20 % donne une image ~30 Go.
    La marge (défaut ×1.25, plancher `floor_bytes`) absorbe les métadonnées du
    mkfs frais, le journal et l'écart statvfs/rsync — et reste au-dessus du
    garde-fou `used × 1.05 > capacité` du moteur. Arrondi au MiO supérieur
    (alignement propre pour sfdisk/losetup).

    La racine de l'image « remplit » ce fichier taillé au plus juste
    (build_dst_script retire le size= de la dernière partition) ; au clonage
    DEPUIS l'image, le même mécanisme la ré-étend à la taille de la vraie cible.
    """
    root = max(int(root_used_bytes * margin), floor_bytes)
    total = p2_start_sectors * 512 + root
    mib = 2**20
    return ((total + mib - 1) // mib) * mib


def rewrite_uboot_script(data, ordered):
    """Patche une image script u-boot (`boot.scr`, magic 0x27051956).

    Remplace les identifiants source par ceux du clone DANS les données, à
    longueur CONSTANTE (UUID, PARTUUID et label-id ont chacun une longueur
    fixe), puis recalcule les deux CRC de l'en-tête legacy uImage : ih_dcrc
    (CRC des données, offset 0x18) et ih_hcrc (CRC de l'en-tête calculé avec
    ce champ à 0, offset 0x04). u-boot rejette une image au CRC faux : sans
    ce patch, un `boot.scr` laissé tel quel ferait booter la racine de la
    SOURCE (clone non bootable).

    Retourne les octets patchés, ou None si l'image est inexploitable ou si
    une substitution changeait la longueur (par prudence, on ne touche pas).
    """
    if len(data) < 64 or data[:4] != b"\x27\x05\x19\x56":
        return None
    header = bytearray(data[:64])
    body = data[64:]
    for old, repl in ordered:
        body = re.sub(re.escape(old.encode()), repl.encode(), body,
                      flags=re.IGNORECASE)
    if body == data[64:]:
        return None                            # rien à remplacer
    if len(body) != len(data) - 64:            # longueur préservée = sûr
        return None
    struct.pack_into(">I", header, 0x18, zlib.crc32(body) & 0xffffffff)   # ih_dcrc
    struct.pack_into(">I", header, 0x04, 0)                               # ih_hcrc
    struct.pack_into(">I", header, 0x04, zlib.crc32(header) & 0xffffffff)
    return bytes(header) + body


def extract_root_ids(data):
    """Identifiants (UUID/PARTUUID) réellement référencés dans une image script
    u-boot (`boot.scr`).

    Sur ODROID, `boot.scr` désigne la racine par `root=UUID=…` (ou
    `root=PARTUUID=…`). On extrait les valeurs présentes pour détecter le cas —
    constaté sur ce projet — où le boot.scr pointe vers un identifiant qui n'est
    NI l'UUID NI le PARTUUID lus par blkid sur la partition racine
    (`root=UUID=eee2b90d…` alors que la partition porte `a9bdb4f9…`). Sans cette
    détection, la table de substitution `_id_subs` (bâtie sur blkid seul)
    raterait ce token et le clone chercherait une racine fantôme.

    Accepte l'image complète (en-tête uImage 0x27051956) ou un corps déjà
    extrait. Retourne une liste de (kind, value) — kind ∈ {"UUID","PARTUUID"} —
    dans l'ordre d'apparition, dédupliquée (casse ignorée).
    """
    if len(data) >= 64 and data[:4] == b"\x27\x05\x19\x56":
        body = data[64:]
    else:
        body = data
    text = body.decode("utf-8", "surrogateescape")
    ids, seen = [], set()
    # PARTUUID avant UUID : `\bUUID=` ne matche PAS dans « PARTUUID= » (pas de
    # frontière de mot entre 'T' et 'U'), donc pas de double capture.
    for kind in ("PARTUUID", "UUID"):
        for m in re.finditer(rf"\b{kind}=([0-9A-Fa-f-]+)", text):
            val = m.group(1)
            key = (kind, val.lower())
            if key not in seen:
                seen.add(key)
                ids.append((kind, val))
    return ids


# Features ext4 activées PAR DÉFAUT par les e2fsprogs récents (>= 1.47) mais
# que le noyau vendor 5.10 de l'ODROID (ou un u-boot ancien) ne gère pas
# forcément : un rootfs formaté avec sur le poste de clonage peut être REFUSÉ au
# montage sur l'unité -> shell (initramfs). D'où le miroir des features de la
# source au mkfs (`parse_ext_features`) + le contrôle d'audit.
RISKY_EXT_FEATURES = frozenset({"orphan_file", "metadata_csum_seed"})


def parse_ext_features(dumpe2fs_output):
    """Ensemble des features d'un fs ext* d'après la sortie de `dumpe2fs -h`
    (ou `tune2fs -l`) : la ligne « Filesystem features: … ». Ensemble vide si
    absente (sortie tronquée, fs non-ext)."""
    m = re.search(r"(?mi)^Filesystem features:\s*(.+)$", dumpe2fs_output or "")
    return set(m.group(1).split()) if m else set()


def storage_modules():
    """Modules de stockage/PCIe/USB chargés sur l'HÔTE.

    L'hôte accède en ce moment au disque cible (il est branché), donc les
    modules qui le pilotent sont chargés : les embarquer dans l'initramfs du
    clone garantit qu'il verra ce disque au démarrage. Robuste car empirique
    (ce qui marche maintenant), plutôt que deviner des noms de modules.
    """
    out = subprocess.run(["lsmod"], capture_output=True, text=True).stdout
    pat = re.compile(r"nvme|pcie|phy|combphy|rockchip|ahci|sata|uas|"
                     r"usb.?storage|xhci|mmc", re.IGNORECASE)
    mods = [line.split()[0] for line in out.splitlines()[1:]
            if line.split() and pat.search(line.split()[0])]
    # nvme_core suit nvme mais peut manquer du filtre selon l'ordre lsmod
    for essential in ("nvme", "nvme_core"):
        if essential not in mods:
            mods.append(essential)
    return mods
