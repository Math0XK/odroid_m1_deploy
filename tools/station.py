#!/usr/bin/env python3
"""
station.py — L'OUTIL du poste de déploiement Odroid-M1. Entrée UNIQUE :
tout passe par ici, en graphique comme en ligne de commande.

SANS argument : tableau de bord graphique plein écran (3 onglets — SPI,
Clone/Image, Vérification), lancé automatiquement au boot par le service
systemd `odroid-station.service` (mode kiosque, voir `../station/`) ou à la
main (`sudo odroid-station`). Échap/F11 bascule le plein écran.

AVEC une sous-commande : les MÊMES opérations en CLI headless (SSH sans X11),
pilotant les mêmes moteurs (`clone_engine.CloneEngine`, `spi_ops.SpiOps`) avec
les mêmes garde-fous — pas de duplication GUI/CLI :

    sudo odroid-station                          # GUI plein écran
    sudo odroid-station list                     # disques détectés
    sudo odroid-station clone --source /dev/sda --dest /dev/nvme0n1
    sudo odroid-station clone --image master.img --dest /dev/nvme0n1
    sudo odroid-station image --source /dev/sda --out master.img
                                                 # image COMPACTE (taillée sur
                                                 # l'espace UTILISÉ, sparse)
    sudo odroid-station spi read                 # puce -> golden (+ SHA256)
    sudo odroid-station spi verify               # puce vs golden
    sudo odroid-station spi flash [--yes]        # golden -> puce (backup avant)
    sudo odroid-station spi env-apply            # 4 vars U-Boot critiques
    sudo odroid-station spi env-save             # dump fw_printenv
    sudo odroid-station check [--npu-cmd "…"]    # GO/NO-GO post-déploiement

`spi --programmer` : `ch341a_spi` (pince CH341A, carte hors tension). Le flash
SPI depuis Linux n'est pas possible (SFC RK3568) ; sans pince, flasher au prompt
U-Boot (`sf write`, cf. docs/DEPLOIEMENT_FLOTTE.md §5). `--sim` journalise les
commandes sans rien exécuter.

La confirmation interactive de `clone` exige de RETAPER le nom du disque de
destination (ex. « sdb ») : sur un poste de flotte on efface des disques à la
chaîne, un simple o/n laisse passer les erreurs d'inattention. `--yes` la
débraye pour les enchaînements scriptés — à réserver aux devices vérifiés.
"""

import argparse
import os
import sys

from clone_core import find_partclone_bundle, list_block_devices
from clone_engine import CloneEngine, assert_not_system_disk
from spi_ops import SpiOps
import check_deploy

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(PKG_ROOT, "images", "spi")
DEFAULT_GOLDEN = os.path.join(GOLDEN_DIR, "golden_spi_16MiB.bin")


# ---------------------------------------------------------------------------
# GUI (par défaut, sans sous-commande) — tkinter importé PARESSEUSEMENT pour
# que le CLI reste utilisable en SSH sans X11 ni python3-tk.
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk
    from spi_panel import SpiPanel
    from clone_panel import ClonePanel
    from verify_panel import VerifyPanel

    class StationGUI(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Station de déploiement Odroid-M1")
            self.option_add("*Font", "TkDefaultFont 12")   # lisible au tactile

            self._fullscreen = True
            self.attributes("-fullscreen", True)
            self.bind("<Escape>", self._toggle_fullscreen)
            self.bind("<F11>", self._toggle_fullscreen)

            header = ttk.Label(self, text="Station de déploiement Odroid-M1",
                               font=("TkDefaultFont", 16, "bold"), anchor="w")
            header.pack(fill="x", padx=10, pady=(8, 0))

            notebook = ttk.Notebook(self)
            notebook.pack(fill="both", expand=True, padx=8, pady=8)
            notebook.add(SpiPanel(notebook), text="SPI (Golden / Flash / Env)")
            notebook.add(ClonePanel(notebook), text="Clone / Image")
            notebook.add(VerifyPanel(notebook), text="Vérification")

        def _toggle_fullscreen(self, _event=None):
            self._fullscreen = not self._fullscreen
            self.attributes("-fullscreen", self._fullscreen)

    if os.geteuid() != 0:
        print("⚠ Pas lancé en root : flash SPI, clonage et fw_setenv échoueront. "
              "Relance avec : sudo odroid-station")
    _fix_x11_env_for_sudo()
    try:
        app = StationGUI()
    except tk.TclError as e:
        print(f"Impossible d'ouvrir la fenêtre graphique : {e}")
        print("Sur le poste kiosque, ce script est lancé par systemd/X — voir "
              "journalctl -u odroid-station -f. En dev, vérifie DISPLAY/X11 "
              "(sudo -E, X11 forwarding). En SSH sans X11, utilise les "
              "sous-commandes : odroid-station --help")
        return 1
    app.mainloop()
    return 0


def _fix_x11_env_for_sudo():
    """sudo n'hérite pas forcément du cookie X11 de l'utilisateur -> Tkinter ne
    peut pas se connecter au display. On repointe XAUTHORITY/DISPLAY vers ceux
    de SUDO_USER.

    Utile pour l'usage manuel/dev (`sudo odroid-station` depuis une session X
    déjà ouverte). Sans effet sur le poste kiosque : là, root démarre X lui-même
    via systemd (même utilisateur de bout en bout, pas de sudo dans la boucle).
    """
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
            import subprocess
            out = subprocess.run(
                ["bash", "-c", f"sudo -u {sudo_user} env | grep ^DISPLAY="],
                capture_output=True, text=True).stdout.strip()
            if out.startswith("DISPLAY="):
                os.environ["DISPLAY"] = out.split("=", 1)[1]
        except Exception:
            pass
        os.environ.setdefault("DISPLAY", ":0")


# ---------------------------------------------------------------------------
# CLI — helpers
# ---------------------------------------------------------------------------
class ProgressPrinter:
    """Progression sur stdout, sans noyer le journal : n'imprime que les
    changements d'au moins 5 points (le poll du moteur tourne à 2 Hz)."""

    def __init__(self):
        self._last = -5.0

    def __call__(self, pct, text):
        if pct - self._last >= 5.0 or pct >= 99.0 and self._last < 99.0:
            self._last = pct
            print(f"[{pct:3.0f} %] {text}", flush=True)


def print_disks():
    disks = list_block_devices()
    if not disks:
        print("Aucun disque détecté.")
        return
    print("Disques détectés :")
    for d in disks:
        suffix = "  [disque système]" if d["system"] else ""
        print(f"  {d['path']:<16} {d['size']:>8}  {d['model']}{suffix}")


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


def confirm_yes(prompt):
    try:
        answer = input(f"{prompt} (o/N) : ")
    except EOFError:
        return False
    return answer.strip().lower() in ("o", "oui")


def _check_source(args, p):
    """Garde-fou source commun clone/image : jamais le disque système (clone à
    froid uniquement)."""
    if args.source:
        try:
            assert_not_system_disk(args.source, role="source")
        except RuntimeError as e:
            p.exit(1, f"ERREUR : {e}\n")


# ---------------------------------------------------------------------------
# CLI — sous-commandes
# ---------------------------------------------------------------------------
def cmd_clone(args, p):
    if not (args.source or args.image):
        p.error("clone : --source ou --image requis.")
    bundle = None
    if args.image:
        if os.path.isdir(args.image):
            bundle = find_partclone_bundle(args.image)
            if bundle is None:
                p.error(f"{args.image} n'est pas un bundle partclone "
                        "(table .sfdisk + images .pc attendues).")
        elif not os.path.isfile(args.image):
            p.error(f"image introuvable : {args.image}")
    if args.source and args.source == args.dest:
        p.error("source et destination identiques.")
    if args.boot_medium and args.boot_medium in (args.source, args.dest):
        p.error("le support de boot doit être un disque DISTINCT de la source "
                "et de la cible.")

    _check_source(args, p)
    try:
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
        if bundle is not None:
            msg = engine.restore_bundle(bundle, args.dest)
        else:
            msg = engine.clone(args.source, args.dest, img_path=args.image)
    except Exception as e:
        print(f"\n=== ERREUR ===\n{e}", file=sys.stderr)
        return 1
    print(f"\n{msg}")
    return 0


def cmd_image(args, p):
    _check_source(args, p)
    if os.path.exists(args.out) and not args.yes:
        if not confirm_yes(f"⚠ {args.out} existe déjà et sera REMPLACÉ. Continuer ?"):
            print("Annulé (confirmation refusée).")
            return 1

    engine = CloneEngine(log=print, progress=ProgressPrinter(),
                         boot_mode=args.boot_mode)
    try:
        msg = engine.make_image(args.source, args.out)
    except Exception as e:
        print(f"\n=== ERREUR ===\n{e}", file=sys.stderr)
        return 1
    print(f"\n{msg}")
    return 0


def cmd_spi(args, p):
    ops = SpiOps(log=print, golden_dir=GOLDEN_DIR, sim=args.sim)
    golden = args.file

    if args.spi_cmd == "read":
        digest = ops.read_golden(args.programmer, golden)
        if digest is not None:
            print(f"\nGolden sauvegardé et vérifié : {golden}")
        return 0

    if args.spi_cmd == "verify":
        if not os.path.isfile(golden) and not args.sim:
            p.error(f"golden absent : {golden}")
        same = ops.verify_chip(args.programmer, golden)
        if same is None:
            return 0
        print("\nPuce IDENTIQUE au golden ✔" if same
              else "\nLa puce DIFFÈRE du golden ✖")
        return 0 if same else 1

    if args.spi_cmd == "flash":
        if not args.sim:
            if not os.path.isfile(golden):
                p.error(f"golden absent : {golden}")
            if not args.yes and not confirm_yes(
                    f"⚠ Ceci EFFACE la puce SPI via « {args.programmer} » et y "
                    f"écrit {golden}.\n(Une sauvegarde de la puce est faite "
                    "avant.) Continuer ?"):
                print("Annulé (confirmation refusée).")
                return 1
        backup = ops.flash_unit(args.programmer, golden)
        if backup is not None:
            print(f"\nFlash réussi et vérifié. Sauvegarde pré-flash : {backup}")
        return 0

    if args.spi_cmd == "env-apply":
        if not args.sim and not args.yes and not confirm_yes(
                "Écrire les 4 variables d'env U-Boot (mtd1) via fw_setenv ?"):
            print("Annulé (confirmation refusée).")
            return 1
        ops.env_apply()
        if not args.sim:
            print("\n4 variables d'env appliquées (mtd1).")
        return 0

    if args.spi_cmd == "env-save":
        path = ops.env_dump()
        if path is not None:
            print(f"\nEnv sauvegardé : {path}")
        return 0

    p.error("sous-commande spi requise : read | verify | flash | env-apply | "
            "env-save")


def cmd_check(args, _p):
    results, go = check_deploy.run_checks(npu_cmd=args.npu_cmd)
    for name, ok, msg in results:
        print(f"[{'OK ' if ok else 'NON'}] {name} : {msg}")
    print(f"\n=== {'GO' if go else 'NO-GO'} ===")
    return 0 if go else 1


# ---------------------------------------------------------------------------
# Parseur
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="odroid-station",
        description="Station de déploiement Odroid-M1 — SANS argument : "
                    "tableau de bord graphique ; avec une sous-commande : les "
                    "mêmes opérations en CLI headless (SSH).")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="liste les disques détectés")

    c = sub.add_parser("clone", help="clone un disque (ou une image) vers un "
                                     "disque cible (EFFACÉ)")
    csrc = c.add_mutually_exclusive_group(required=False)
    csrc.add_argument("--source", metavar="DEV",
                      help="disque source (à froid : l'Odroid source éteint, "
                           "branché en lecteur USB)")
    csrc.add_argument("--image", metavar="FICHIER|DOSSIER",
                      help="image source : fichier .img (monté en loop device), "
                           "OU dossier d'une sauvegarde partclone (table .sfdisk "
                           "+ images .pc)")
    c.add_argument("--dest", metavar="DEV", required=True,
                   help="disque de destination (sera EFFACÉ)")
    c.add_argument("--boot-mode", choices=["spi", "disk"], default="spi",
                   help="mode de boot de la cible (défaut : spi — u-boot en "
                        "puce SPI, flotte ; 'disk' = legacy eMMC/SD "
                        "auto-bootable, recopie idbloader/u-boot)")
    c.add_argument("--boot-medium", metavar="DEV", default=None,
                   help="(legacy, SSD USB-SATA uniquement) support de boot "
                        "séparé USB/SD, sera EFFACÉ. Inutile pour un NVMe.")
    c.add_argument("--yes", action="store_true",
                   help="saute la confirmation interactive (usage scripté)")

    i = sub.add_parser("image", help="crée une image disque COMPACTE d'un "
                                     "disque (future source de clonage)")
    i.add_argument("--source", metavar="DEV", required=True,
                   help="disque à imager")
    i.add_argument("--out", metavar="FICHIER", required=True,
                   help="fichier image à créer (taillé sur l'espace UTILISÉ de "
                        "la racine, sparse)")
    i.add_argument("--boot-mode", choices=["spi", "disk"], default="spi",
                   help="mode de boot embarqué dans l'image (défaut : spi ; "
                        "'disk' recopie la zone idbloader/u-boot dans l'image)")
    i.add_argument("--yes", action="store_true",
                   help="remplace un fichier existant sans confirmation")

    s = sub.add_parser("spi", help="puce SPI : read | verify | flash | "
                                   "env-apply | env-save")
    s.add_argument("spi_cmd", nargs="?",
                   choices=["read", "verify", "flash", "env-apply", "env-save"],
                   help="opération SPI")
    s.add_argument("--programmer", default="ch341a_spi",
                   help="programmer flashrom (défaut ch341a_spi : pince CH341A, "
                        "carte hors tension). Sans pince : flasher au prompt "
                        "U-Boot (sf write, cf. runbook §5)")
    s.add_argument("--file", metavar="FICHIER", default=DEFAULT_GOLDEN,
                   help=f"image SPI 16 MiO (défaut : {DEFAULT_GOLDEN})")
    s.add_argument("--sim", action="store_true",
                   help="simulation : journalise les commandes sans les exécuter")
    s.add_argument("--yes", action="store_true",
                   help="saute la confirmation interactive (flash/env-apply)")

    k = sub.add_parser("check", help="vérification post-déploiement GO/NO-GO "
                                     "(à lancer SUR l'unité)")
    k.add_argument("--npu-cmd",
                   help="commande de benchmark NPU à lancer (optionnel), ex. "
                        "\"python3 infer_rknn.py --model m.rknn --benchmark "
                        "--runs 20\"")
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)

    if args.cmd is None:
        return run_gui()

    # Root requis pour tout ce qui touche disques bruts / puce SPI / env U-Boot
    # (la simulation SPI et list/check tournent sans).
    needs_root = args.cmd in ("clone", "image") or \
        (args.cmd == "spi" and not args.sim)
    if needs_root and os.geteuid() != 0:
        print(f"Lance avec : sudo odroid-station {args.cmd} … "
              "(accès disques bruts / puce SPI requis)", file=sys.stderr)
        return 1

    if args.cmd == "list":
        print_disks()
        return 0
    if args.cmd == "clone":
        return cmd_clone(args, p)
    if args.cmd == "image":
        return cmd_image(args, p)
    if args.cmd == "spi":
        return cmd_spi(args, p)
    if args.cmd == "check":
        return cmd_check(args, p)
    p.error(f"sous-commande inconnue : {args.cmd}")


if __name__ == "__main__":
    if os.name != "posix":
        print("Outil Linux (poste de déploiement Odroid-M1) : lsblk, sfdisk, "
              "mount, rsync, flashrom…")
        sys.exit(1)

    # util-linux >= 2.39 passe par la nouvelle API de montage du noyau, qui
    # renvoie des « mount failed: Operation not permitted » injustifiés dans
    # certains environnements (WSL2, noyaux BSP...). On force l'appel mount(2)
    # classique : sans effet là où tout va bien, corrige le reste.
    os.environ.setdefault("LIBMOUNT_FORCE_MOUNT2", "always")

    sys.exit(main())
