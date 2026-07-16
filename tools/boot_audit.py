#!/usr/bin/env python3
"""
boot_audit.py — audit de bootabilité d'un clone, APRÈS écriture, AVANT de le
déclarer bon. Répond à LA question : « ce disque va-t-il trouver sa racine au
premier boot ? » — sur le poste de clonage, sans brancher l'ODROID.

Né d'un échec réel : un clone dont le `boot.scr` avait été régénéré par
flash-kernel (hook d'`update-initramfs` dans le chroot) avec le `root=UUID`
PÉRIMÉ de la config du master -> kernel démarré, racine introuvable, shell
`(initramfs)`. Chaque contrôle ci-dessous correspond à un mode d'échec
observé ou plausible :

  1. `boot.scr` présent et CRC uImage valides (u-boot rejette un CRC faux) ;
  2. chaque `root=UUID/PARTUUID` du `boot.scr` existe RÉELLEMENT sur le clone ;
  3. les bootargs contiennent les jetons attendus (`cma=128M` : NPU) ;
  4. le fstab ne référence que des identifiants du clone ;
  5. kernel + initrd présents sur la partition BOOT ;
  6. la config flash-kernel (`/etc/default/flash-kernel`, `/etc/flash-kernel/`)
     ne référence plus d'identifiant étranger — sinon le PROCHAIN
     `update-initramfs` sur l'unité régénérerait un boot.scr cassé ;
  7. les features ext du clone n'excèdent pas celles de la source (un mkfs
     récent peut activer `orphan_file`/`metadata_csum_seed`, indigestes pour un
     noyau 5.10 ou un vieux u-boot).

Chaque contrôle rend un `AuditCheck(name, ok, severity, msg)` ; severity
« fail » = le clone ne bootera pas (NO-GO), « warn » = boote mais dégradé /
fragile. Les fonctions PURES (bytes/texte en entrée) sont testées sans
matériel (`tests/test_boot_audit.py`) ; seul `run_audit` touche le disque.
"""

import glob
import os
import re
import struct
import zlib
from collections import namedtuple

from clone_core import RISKY_EXT_FEATURES, extract_root_ids

AuditCheck = namedtuple("AuditCheck", "name ok severity msg")

SEV_FAIL = "fail"     # le clone ne bootera pas
SEV_WARN = "warn"     # boote, mais dégradé ou fragile


# ---------------------------------------------------------------------------
# Contrôles purs (bytes/texte en entrée — testables sans matériel)
# ---------------------------------------------------------------------------
def uimage_crc_ok(data):
    """CRC d'une image legacy uImage (boot.scr) valides ? u-boot recalcule
    ih_dcrc (données) et ih_hcrc (en-tête) et REFUSE l'image au moindre écart —
    un boot.scr mal patché est donc invisible au boot, pas juste bancal."""
    if len(data) < 64 or data[:4] != b"\x27\x05\x19\x56":
        return False
    header = bytearray(data[:64])
    size = struct.unpack(">I", header[0x0C:0x10])[0]
    body = data[64:]
    if size > len(body):
        return False
    stored_dcrc = struct.unpack(">I", header[0x18:0x1C])[0]
    # dcrc calculé sur ih_size octets ; tolère aussi un corps exact (les deux
    # conventions existent selon l'outil qui a produit/patché l'image).
    if stored_dcrc not in (zlib.crc32(body[:size]) & 0xffffffff,
                           zlib.crc32(body) & 0xffffffff):
        return False
    stored_hcrc = struct.unpack(">I", header[0x04:0x08])[0]
    struct.pack_into(">I", header, 0x04, 0)
    return stored_hcrc == (zlib.crc32(bytes(header)) & 0xffffffff)


def ids_in_text(text):
    """(kind, value) de chaque jeton UUID=… / PARTUUID=… d'un texte de config
    (fstab, flash-kernel…), dédupliqués, ordre d'apparition."""
    ids, seen = [], set()
    for kind in ("PARTUUID", "UUID"):
        for m in re.finditer(rf"\b{kind}=([0-9A-Fa-f-]+)", text or ""):
            key = (kind, m.group(1).lower())
            if key not in seen:
                seen.add(key)
                ids.append((kind, m.group(1)))
    return ids


def foreign_ids(found_ids, valid_ids):
    """Identifiants présents dans une config mais INCONNUS du clone (liste de
    chaînes « KIND=valeur », vide = tout est cohérent). `valid_ids` : ensemble
    de valeurs en minuscules (UUID et PARTUUID confondus)."""
    return [f"{kind}={val}" for kind, val in found_ids
            if val.lower() not in valid_ids]


def check_bootscr(data, valid_ids, expect_tokens=()):
    """Contrôles 1-3 sur le contenu d'un boot.scr (bytes). Liste d'AuditCheck."""
    checks = []
    crc = uimage_crc_ok(data)
    checks.append(AuditCheck(
        "boot.scr : CRC uImage", crc, SEV_FAIL,
        "valides (u-boot l'acceptera)" if crc else
        "INVALIDES — u-boot refusera ce script (image corrompue ?)"))

    roots = extract_root_ids(data)
    bad = foreign_ids(roots, valid_ids)
    if not roots:
        checks.append(AuditCheck(
            "boot.scr : root=", False, SEV_WARN,
            "aucun root=UUID/PARTUUID trouvé — racine désignée autrement ? "
            "à vérifier manuellement"))
    else:
        pretty = ", ".join(f"{k}={v}" for k, v in roots)
        checks.append(AuditCheck(
            "boot.scr : root=", not bad, SEV_FAIL,
            f"{pretty} — tous présents sur le clone" if not bad else
            f"référence des identifiants INEXISTANTS sur le clone : "
            f"{', '.join(bad)} (la racine ne sera JAMAIS trouvée -> shell initramfs)"))

    body = data[64:] if data[:4] == b"\x27\x05\x19\x56" else data
    for token in expect_tokens:
        present = token.encode() in body
        checks.append(AuditCheck(
            f"boot.scr : {token}", present, SEV_WARN,
            "présent dans les bootargs" if present else
            f"ABSENT des bootargs — sur ce projet {token} conditionne le NPU "
            "(cma) : à réparer avant déploiement"))
    return checks


def check_fstab(fstab_text, valid_ids):
    """Contrôle 4 : chaque UUID/PARTUUID du fstab appartient au clone."""
    found = ids_in_text(fstab_text)
    bad = foreign_ids(found, valid_ids)
    if not found:
        return [AuditCheck("fstab", True, SEV_WARN,
                           "aucun montage par UUID/PARTUUID (par device ?)")]
    return [AuditCheck(
        "fstab", not bad, SEV_FAIL,
        f"{len(found)} identifiant(s), tous du clone" if not bad else
        f"identifiants étrangers : {', '.join(bad)} (montages cassés au boot)")]


def check_regen_config(texts_by_path, valid_ids):
    """Contrôle 6 : configs qui SERVENT À RÉGÉNÉRER le boot (flash-kernel…).
    Un identifiant étranger là-dedans ne casse pas CE boot, mais le prochain
    `update-initramfs` sur l'unité régénérerait un boot.scr pointant dessus."""
    checks = []
    for path, text in texts_by_path.items():
        bad = foreign_ids(ids_in_text(text), valid_ids)
        if bad:
            checks.append(AuditCheck(
                f"config de régénération : {path}", False, SEV_WARN,
                f"référence {', '.join(bad)} — le prochain update-initramfs "
                "sur l'unité régénérerait un boot.scr cassé"))
    if not checks:
        checks.append(AuditCheck(
            "configs de régénération (flash-kernel…)", True, SEV_WARN,
            "aucun identifiant étranger"))
    return checks


def check_ext_features(src_feats, dst_feats, part_label):
    """Contrôle 7 : features ext du clone ⊆ features de la source. Les extras
    « risqués » (RISKY_EXT_FEATURES) sont bloquants : le noyau 5.10 de l'unité
    peut refuser le montage."""
    if not src_feats or not dst_feats:
        return [AuditCheck(f"features ext ({part_label})", True, SEV_WARN,
                           "non comparées (dumpe2fs indisponible ?)")]
    extra = dst_feats - src_feats
    if not extra:
        return [AuditCheck(f"features ext ({part_label})", True, SEV_WARN,
                           "identiques à la source")]
    risky = extra & RISKY_EXT_FEATURES
    sev = SEV_FAIL if risky else SEV_WARN
    return [AuditCheck(
        f"features ext ({part_label})", False, sev,
        f"features EN PLUS de la source : {', '.join(sorted(extra))}"
        + (f" — {', '.join(sorted(risky))} peut être REFUSÉ par le noyau 5.10 "
           "de l'unité" if risky else " (a priori inoffensif)"))]


def check_boot_files(filenames):
    """Contrôle 5 : un kernel et un initrd existent sur la partition BOOT."""
    names = set(filenames)
    kernel = [n for n in names
              if n.startswith(("vmlinuz", "Image", "zImage", "vmlinux"))]
    initrd = [n for n in names if n.startswith(("uInitrd", "initrd"))]
    checks = [AuditCheck(
        "BOOT : noyau", bool(kernel), SEV_FAIL,
        f"présent ({', '.join(sorted(kernel)[:3])})" if kernel else
        "AUCUN fichier noyau (vmlinuz/Image…) sur la partition BOOT")]
    checks.append(AuditCheck(
        "BOOT : initrd", bool(initrd), SEV_FAIL,
        f"présent ({', '.join(sorted(initrd)[:3])})" if initrd else
        "AUCUN initrd/uInitrd sur la partition BOOT"))
    return checks


def verdict(checks):
    """(go, n_fails, n_warns) d'une liste d'AuditCheck : GO si aucun contrôle
    de sévérité « fail » n'a échoué."""
    fails = [c for c in checks if not c.ok and c.severity == SEV_FAIL]
    warns = [c for c in checks if not c.ok and c.severity == SEV_WARN]
    return (not fails), len(fails), len(warns)


# ---------------------------------------------------------------------------
# Runner (le seul à toucher les fichiers du clone monté)
# ---------------------------------------------------------------------------
# Configs du rootfs qui alimentent une future régénération du boot.
REGEN_CONFIG_GLOBS = (
    "etc/default/flash-kernel",
    "etc/flash-kernel/*",
    "etc/flash-kernel/ubootenv.d/*",
    "etc/default/u-boot",
)


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def find_bootscr(boot_mp, root_mp):
    """Chemin du boot.scr ACTIF du clone (partition BOOT en premier — c'est là
    qu'u-boot le lit sur ce projet), ou None."""
    for p in (os.path.join(boot_mp, "boot.scr"),
              os.path.join(root_mp, "boot", "boot.scr")):
        if os.path.isfile(p):
            return p
    return None


def run_audit(root_mp, boot_mp, valid_ids, expect_tokens=("cma=128M",),
              src_feats=None, dst_feats=None):
    """Audit complet d'un clone MONTÉ (root_mp/boot_mp). Retourne la liste
    d'AuditCheck (voir `verdict` pour le GO/NO-GO).

    `valid_ids` : toutes les valeurs UUID/PARTUUID du clone, minuscules.
    `src_feats`/`dst_feats` : dicts optionnels {label -> set de features ext}
    de mêmes clés (ex. {"BOOT": {...}, "rootfs": {...}}).
    """
    checks = []

    scr = find_bootscr(boot_mp, root_mp)
    if scr is None:
        checks.append(AuditCheck(
            "boot.scr", False, SEV_FAIL,
            "INTROUVABLE sur la partition BOOT (et <root>/boot) — u-boot n'aura "
            "rien à exécuter"))
    else:
        data = _read_bytes(scr) or b""
        checks.extend(check_bootscr(data, valid_ids, expect_tokens))

    fstab = _read_bytes(os.path.join(root_mp, "etc", "fstab"))
    if fstab is None:
        checks.append(AuditCheck("fstab", False, SEV_FAIL,
                                 "/etc/fstab illisible sur le clone"))
    else:
        checks.extend(check_fstab(fstab.decode("utf-8", "surrogateescape"),
                                  valid_ids))

    try:
        boot_files = os.listdir(boot_mp)
    except OSError:
        boot_files = []
    checks.extend(check_boot_files(boot_files))

    regen = {}
    for pat in REGEN_CONFIG_GLOBS:
        for p in glob.glob(os.path.join(root_mp, pat)):
            if os.path.isfile(p):
                data = _read_bytes(p)
                if data is not None and b"\x00" not in data[:4096]:
                    rel = os.path.relpath(p, root_mp)
                    regen[rel] = data.decode("utf-8", "surrogateescape")
    checks.extend(check_regen_config(regen, valid_ids))

    for label in sorted((src_feats or {}).keys() | (dst_feats or {}).keys()):
        checks.extend(check_ext_features((src_feats or {}).get(label),
                                         (dst_feats or {}).get(label), label))
    return checks
