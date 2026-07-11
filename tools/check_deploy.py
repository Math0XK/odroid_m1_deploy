#!/usr/bin/env python3
"""
check_deploy.py — vérification post-déploiement d'une unité Odroid-M1.

À lancer **SUR l'unité** fraîchement flashée (boot NVMe direct via la puce SPI).
Confirme automatiquement que le système est sain (tâche #4 de la note de
passation) : kernel vendor attendu, racine sur NVMe, absence des erreurs NPU
connues dans dmesg, nœuds DRI présents, et — en option — un smoke test
d'inférence NPU.

Deux usages, même logique :
  - CLI headless (ex. en SSH, après un flash à la pince depuis un banc) :
        sudo python3 check_deploy.py
        sudo python3 check_deploy.py --npu-cmd "python3 infer_rknn.py \\
            --model best_rknn_model/best.rknn --benchmark --runs 20"
  - importé par la GUI `spi_flash_gui.py` (section « Vérif déploiement ») via
    `run_checks()`.

Les PARSERS (pur, testés) vivent dans `spi_core` ; ici on ne fait que lancer les
commandes système et agréger le GO/NO-GO. `dmesg` nécessite souvent root -> lancer
en `sudo`, sinon le contrôle dmesg est marqué illisible.
"""

import argparse
import os
import subprocess
import sys

import spi_core as sc


def _run(argv):
    """stdout+stderr d'une commande, '' si absente/échec (les contrôles gèrent le
    vide). `argv` : liste d'arguments."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
        return (r.stdout or "") + (r.stderr or "")
    except (FileNotFoundError, OSError):
        return ""


def run_checks(npu_cmd=None):
    """Lance les contrôles et retourne (results, go).

    results : liste de (nom, ok: bool, message). go : True si TOUS les contrôles
    lancés sont OK (le smoke test NPU n'entre dans le GO que s'il est demandé).
    `npu_cmd` : chaîne ou liste d'arguments du benchmark NPU, ou None.
    """
    results = []

    ok, msg = sc.check_kernel(_run(["uname", "-r"]))
    results.append(("Kernel vendor", ok, msg))

    src = _run(["findmnt", "-n", "-o", "SOURCE", "/"])
    results.append(("Racine sur NVMe", sc.root_on_nvme(src), src.strip() or "?"))

    dmesg = _run(["dmesg"])
    if not dmesg.strip():
        results.append(("dmesg sans erreur NPU", False, "dmesg illisible (lance en sudo)"))
    else:
        errs = sc.scan_dmesg_npu_errors(dmesg)
        results.append(("dmesg sans erreur NPU", not errs,
                        "aucune" if not errs else f"{len(errs)} : {errs[0]}"))

    existing = [n for n in sc.REQUIRED_DRI_NODES if os.path.exists(n)]
    missing = sc.missing_dri_nodes(existing)
    results.append(("Nœuds DRI", not missing,
                    "présents" if not missing else f"manquants : {', '.join(missing)}"))

    # Smoke test NPU (optionnel) : compté dans le GO seulement s'il est demandé.
    if npu_cmd:
        argv = npu_cmd if isinstance(npu_cmd, list) else npu_cmd.split()
        ms = sc.parse_inference_ms(_run(argv))
        if ms is None:
            results.append(("Test NPU", False, "aucune latence lue dans la sortie"))
        else:
            results.append(("Test NPU", ms < 100.0,
                            f"{ms:.1f} ms/run" + ("" if ms < 100.0 else " (>100 ms !)")))

    go = all(ok for _, ok, _ in results)
    return results, go


def main(argv=None):
    p = argparse.ArgumentParser(description="Vérif post-déploiement Odroid-M1.")
    p.add_argument("--npu-cmd",
                   help="commande de benchmark NPU à lancer (optionnel), ex. "
                        "\"python3 infer_rknn.py --model m.rknn --benchmark --runs 20\"")
    args = p.parse_args(argv)

    results, go = run_checks(npu_cmd=args.npu_cmd)
    for name, ok, msg in results:
        print(f"[{'OK ' if ok else 'NON'}] {name} : {msg}")
    print(f"\n=== {'GO' if go else 'NO-GO'} ===")
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())
