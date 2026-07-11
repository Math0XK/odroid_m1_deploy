#!/usr/bin/env python3
"""
spi_core.py — logique PURE (sans interface ni matériel) de la chaîne SPI Odroid.

Séparé de `spi_flash_gui.py` (l'interface tkinter + les appels flashrom/fw_setenv)
sur le même principe que `clone_core.py` / `clone_odroid_gui.py` : tout ce qui est
testable sans puce SPI ni flashrom vit ici (`tests/test_spi_core.py`).

Couvre :
  - la sauvegarde/flash de la puce SPI 16 MiO qui porte le bootloader Rockchip
    (idbloader-spi + u-boot 2026.01 Armbian + env U-Boot) — voir la note de
    passation `docs/odroid-m1-boot-nvme-npu-handoff.md` ;
  - les garde-fous anti-brique (taille, image non vierge, signature U-Boot) ;
  - les 4 variables d'env U-Boot critiques à ré-appliquer après un flash d'env
    d'origine, et leur traduction en commandes `fw_setenv` ;
  - les parsers de la vérification post-déploiement (kernel, racine NVMe, erreurs
    NPU dans dmesg, nœuds DRI, latence d'inférence).
"""

import hashlib
import re

# Taille exacte de la puce SPI NOR de l'Odroid-M1 (16 MiO, 5 partitions MTD).
# Un dump qui n'a pas EXACTEMENT cette taille est suspect (lecture partielle,
# mauvaise puce) et doit être refusé avant tout flash.
SPI_SIZE = 16 * 1024 * 1024

# Les 4 variables d'env U-Boot ajoutées pour faire booter le NVMe directement
# depuis la SPI (note de passation). À ré-appliquer via `fw_setenv` APRÈS un
# reflash de l'env d'origine (mtd1). Un flash de l'IMAGE COMPLÈTE (16 MiO) les
# embarque déjà (mtd1 inclus) : `fw_setenv` n'est alors qu'un filet de rattrapage.
CRITICAL_ENV_VARS = {
    "boot_targets": "nvme mmc1 mmc0 mtd2 mtd1 mtd0 usb0 pxe dhcp",
    "npu_regulator_enable":
        "regulator dev vdd_npu; regulator value 900000; regulator enable",
    "nvme_boot":
        "run npu_regulator_enable; pci enum; nvme scan; "
        "if nvme device ${devnum}; then setenv devtype nvme; "
        "run scan_dev_for_boot_part; fi",
    "bootcmd_nvme": "setenv devnum 0; run nvme_boot",
}

# Kernel vendor attendu sur une unité saine (driver NPU RKNPU).
EXPECTED_KERNEL = "5.10.0-odroid-arm64"

# Erreurs connues qui signent une régression du NPU au boot (note de passation,
# root causes #2 et #3). Leur ABSENCE dans dmesg fait partie du GO.
NPU_ERROR_PATTERNS = (
    "failed to get ack",     # régulateur vdd_npu non activé -> panic domaine NPU
    "failed to allocate",    # CMA insuffisant (cma=128M manquant)
)

# Nœuds DRI que le driver GPU/NPU doit exposer une fois le système sain.
REQUIRED_DRI_NODES = ("/dev/dri/card0", "/dev/dri/renderD128")


# --------------------------------------------------------------------------
# Empreintes / intégrité
# --------------------------------------------------------------------------
def sha256_bytes(data):
    """SHA-256 hex d'un buffer."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=1 << 20):
    """SHA-256 hex d'un fichier, lu par blocs (le golden fait 16 MiO)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def looks_like_bootloader(data):
    """Garde-fou anti-brique AVANT tout flash : `data` ressemble-t-il à une image
    SPI bootloader Odroid-M1 valide ? Retourne (ok: bool, raison: str).

    Contrôles volontairement indépendants du format SPI exact (l'idbloader-spi
    subit un ré-agencement par pages 2 Kio propre au format `rkspi`, dont le
    magic n'est pas à un offset fiable) :
      - taille EXACTE de 16 MiO ;
      - image non uniforme : ni tout à 0x00 (lecture ratée) ni tout à 0xFF
        (puce effacée / vierge) ;
      - présence de la bannière ASCII « U-Boot » (le binaire u-boot la contient
        toujours, ex. « U-Boot 2026.01 »).
    Refuser un golden qui échoue ici évite de propager une brique à la flotte.
    """
    if len(data) != SPI_SIZE:
        return False, f"taille {len(data)} octets != {SPI_SIZE} (16 MiO attendus)"
    if data.count(0) == len(data):
        return False, "image entièrement nulle (lecture flashrom ratée ?)"
    if data.count(0xFF) == len(data):
        return False, "image entièrement 0xFF (puce effacée / vierge)"
    if b"U-Boot" not in data:
        return False, "aucune bannière « U-Boot » trouvée (pas une image bootloader ?)"
    return True, "OK — 16 MiO, non uniforme, bannière U-Boot présente"


# --------------------------------------------------------------------------
# Construction de commandes (testable sans exécuter flashrom / fw_setenv)
# --------------------------------------------------------------------------
def flashrom_cmd(op, programmer, path, chip=None):
    """argv flashrom pour une opération, sans l'exécuter.

    op ∈ {"read","write","verify"} ; `programmer` est passé tel quel à `-p`
    (`internal` ou `linux_mtd:dev=0` en on-device, `ch341a_spi` à la pince).
    `chip` (option `-c`) n'est utile que si l'auto-détection échoue.
    """
    flag = {"read": "-r", "write": "-w", "verify": "-v"}[op]
    cmd = ["flashrom", "-p", programmer]
    if chip:
        cmd += ["-c", chip]
    cmd += [flag, path]
    return cmd


def fw_setenv_commands(env=None):
    """Liste d'argv `fw_setenv` pour (ré)appliquer les variables d'env critiques.

    Une commande par variable : `["fw_setenv", nom, valeur]`. À lancer en root
    sur l'unité (écrit dans mtd1 « U-Boot Env »), APRÈS un reflash de l'env
    d'origine — cf. CRITICAL_ENV_VARS.
    """
    env = CRITICAL_ENV_VARS if env is None else env
    return [["fw_setenv", name, value] for name, value in env.items()]


def env_has_ethaddr(env_text):
    """Vrai si la sortie `fw_printenv` contient une MAC figée (`ethaddr=`).

    Important avant un flash d'IMAGE COMPLÈTE : si le master a une `ethaddr`
    sauvegardée dans son env, la cloner tel quel donnerait la MÊME MAC à toute la
    flotte (collision réseau). Le RK3568 dérive normalement sa MAC de l'efuse,
    mais on ne le suppose pas — on avertit si `ethaddr` est présent.
    """
    return bool(re.search(r"(?mi)^\s*ethaddr\s*=", env_text))


# --------------------------------------------------------------------------
# Parsers de vérification post-déploiement (task #4 de la passation)
# --------------------------------------------------------------------------
def check_kernel(uname_r):
    """(ok, msg) : le kernel courant est-il le vendor attendu ?"""
    got = (uname_r or "").strip()
    if got == EXPECTED_KERNEL:
        return True, f"kernel {got}"
    return False, f"kernel {got or '?'} (attendu {EXPECTED_KERNEL})"


def root_on_nvme(findmnt_source):
    """Vrai si la sortie de `findmnt -n -o SOURCE /` désigne une partition NVMe."""
    return (findmnt_source or "").strip().startswith("/dev/nvme")


def scan_dmesg_npu_errors(dmesg_text):
    """Lignes de dmesg qui matchent une régression NPU connue (liste possiblement
    vide = bon signe)."""
    hits = []
    for line in (dmesg_text or "").splitlines():
        low = line.lower()
        if any(pat in low for pat in NPU_ERROR_PATTERNS):
            hits.append(line.strip())
    return hits


def missing_dri_nodes(existing_paths):
    """Nœuds DRI requis absents de `existing_paths` (liste vide = tous présents)."""
    have = set(existing_paths)
    return [node for node in REQUIRED_DRI_NODES if node not in have]


def parse_inference_ms(text):
    """Latence d'inférence (ms) extraite d'une sortie de benchmark, ou None.

    Cherche le dernier motif « <nombre> ms » (moyenne/médiane affichée en fin de
    run par `infer_rknn.py --benchmark` comme par `rknn_yolov5_demo`). Lenient à
    dessein : on ne se couple pas au format exact d'un seul outil.
    """
    vals = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*ms\b", text or "")
    return float(vals[-1]) if vals else None
