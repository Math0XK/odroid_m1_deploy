#!/usr/bin/env python3
"""
clone_cli.py — clonage de disque Odroid en LIGNE DE COMMANDE (headless).

Même moteur que la GUI (`clone_engine.CloneEngine`, mêmes garde-fous, mêmes
étapes — voir le docstring de `clone_odroid_gui.py` pour le POURQUOI de
chacune), mais pilotable en SSH sans X11 : c'est le chemin normal sur un Odroid
vierge fraîchement installé par `install.sh`, avant tout écran/bureau.

Usage :
    sudo odroid-clone-cli --list
    sudo odroid-clone-cli --source /dev/sda --dest /dev/nvme0n1
    sudo odroid-clone-cli --image master.img --dest /dev/sdb --boot-mode disk
    sudo odroid-clone-cli --source /dev/sda --dest /dev/sdb \\
        --boot-medium /dev/sdc          # legacy SSD USB-SATA uniquement
    ... --yes                           # saute la confirmation (scripts/flotte)

La confirmation interactive exige de RETAPER le nom du disque de destination
(ex. « sdb ») : sur un poste de flotte on efface des disques à la chaîne, un
simple o/n laisse passer les erreurs d'inattention. `--yes` la débraye pour
les enchaînements scriptés — à réserver aux devices vérifiés en amont.
"""

import argparse
import os
import sys

from clone_core import list_block_devices
from clone_engine import CloneEngine, assert_not_system_disk


def print_disks():
    disks = list_block_devices()
    if not disks:
        print("Aucun disque détecté.")
        return
    print("Disques détectés :")
    for d in disks:
        suffix = "  [disque système — REFUSÉ en source/destination]" if d["system"] else ""
        print(f"  {d['path']:<16} {d['size']:>8}  {d['model']}{suffix}")


class ProgressPrinter:
    """Progression sur stdout, sans noyer le journal : n'imprime que les
    changements d'au moins 5 points (le poll du moteur tourne à 2 Hz)."""

    def __init__(self):
        self._last = -5.0

    def __call__(self, pct, text):
        if pct - self._last >= 5.0 or pct >= 99.0 and self._last < 99.0:
            self._last = pct
            print(f"[{pct:3.0f} %] {text}", flush=True)


def confirm_destruction(dst_disk, boot_medium):
    """Confirmation interactive : retaper le nom court du disque cible."""
    expected = os.path.basename(dst_disk)
    print(f"\n⚠ Ceci va EFFACER TOUT LE CONTENU de {dst_disk}"
          + (f"\n  ET de {boot_medium} (support de boot)" if boot_medium else ""))
    try:
        answer = input(f"Pour confirmer, tape le nom du disque cible ({expected}) : ")
    except EOFError:
        return False
    return answer.strip() == expected


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Clonage de disque Odroid (headless). GUI équivalente : "
                    "clone_odroid_gui.py / odroid-clone.")
    p.add_argument("--list", action="store_true",
                   help="liste les disques détectés puis quitte")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--source", metavar="DEV",
                     help="disque source (ex. /dev/sda — l'Odroid éteint, branché "
                          "en lecteur USB : clone à froid uniquement)")
    src.add_argument("--image", metavar="FICHIER",
                     help="fichier image source (.img), monté en loop device")
    p.add_argument("--dest", metavar="DEV",
                   help="disque de destination (sera EFFACÉ)")
    p.add_argument("--boot-mode", choices=["spi", "disk"], default="spi",
                   help="mode de boot de la cible (défaut : spi — u-boot en puce "
                        "SPI, flotte ; 'disk' = legacy eMMC/SD auto-bootable, "
                        "recopie idbloader/u-boot)")
    p.add_argument("--boot-medium", metavar="DEV", default=None,
                   help="(legacy, SSD USB-SATA uniquement) support de boot séparé "
                        "USB/SD, sera EFFACÉ. Inutile pour un NVMe.")
    p.add_argument("--yes", action="store_true",
                   help="saute la confirmation interactive (usage scripté)")
    args = p.parse_args(argv)

    if args.list:
        print_disks()
        return 0

    if not args.dest or not (args.source or args.image):
        p.error("--dest et (--source ou --image) sont requis (ou --list). "
                "Voir --help.")

    if args.image and not os.path.isfile(args.image):
        p.error(f"image introuvable : {args.image}")
    if args.source and args.source == args.dest:
        p.error("source et destination identiques.")
    if args.boot_medium and args.boot_medium in (args.source, args.dest):
        p.error("le support de boot doit être un disque DISTINCT de la source "
                "et de la cible.")

    # Mêmes garde-fous que la GUI : jamais le disque système, ni en source
    # (clone à froid uniquement) ni en destination/support de boot.
    try:
        if args.source:
            assert_not_system_disk(args.source, role="source")
        assert_not_system_disk(args.dest, role="destination")
        if args.boot_medium:
            assert_not_system_disk(args.boot_medium, role="support de boot")
    except RuntimeError as e:
        print(f"ERREUR : {e}", file=sys.stderr)
        return 1

    if not args.yes and not confirm_destruction(args.dest, args.boot_medium):
        print("Annulé (confirmation refusée).")
        return 1

    engine = CloneEngine(log=print, progress=ProgressPrinter(),
                         boot_mode=args.boot_mode, boot_medium=args.boot_medium)
    try:
        msg = engine.clone(args.source, args.dest, img_path=args.image)
    except Exception as e:
        print(f"\n=== ERREUR ===\n{e}", file=sys.stderr)
        return 1
    print(f"\n{msg}")
    return 0


if __name__ == "__main__":
    if os.name != "posix":
        print("Cet outil doit tourner sous Linux : il utilise lsblk, sfdisk, "
              "mount et rsync.")
        sys.exit(1)
    if os.geteuid() != 0 and "--list" not in sys.argv and "--help" not in sys.argv \
            and "-h" not in sys.argv:
        print("Lance avec : sudo odroid-clone-cli … (accès disques bruts requis)")
        sys.exit(1)

    # util-linux >= 2.39 passe par la nouvelle API de montage du noyau, qui
    # renvoie des « mount failed: Operation not permitted » injustifiés dans
    # certains environnements (WSL2, noyaux BSP...). On force l'appel mount(2)
    # classique : sans effet là où tout va bien, corrige le reste.
    os.environ.setdefault("LIBMOUNT_FORCE_MOUNT2", "always")

    sys.exit(main())
