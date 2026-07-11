#!/usr/bin/env python3
"""
station_gui.py — tableau de bord unifié du poste de déploiement Odroid-M1.

Point d'entrée du poste DÉDIÉ (« ne sert qu'à ça ») : lancé automatiquement au
boot par le service systemd `odroid-station.service` (voir `../station/`), qui
démarre X en mode kiosque (pas de gestionnaire de bureau) sur une image
Ubuntu/Armbian minimale. Fenêtre plein écran avec 3 onglets, chacun un `ttk.Frame`
réutilisé tel quel depuis les outils individuels (même code, pas de duplication) :

  - « SPI (Golden / Flash / Env) »  -> `spi_flash_gui.SpiPanel`
  - « Clone NVMe »                  -> `clone_odroid_gui.ClonePanel`
  - « Vérification »                -> `verify_panel.VerifyPanel`

Chaque onglet garde son propre thread de travail / file UI / état "occupé" —
indépendants les uns des autres (pas de verrou inter-onglets pour l'instant :
chaque panel désactive déjà ses propres boutons pendant une opération).

Root démarre X lui-même via systemd (voir `../station/odroid-station.service`) :
même utilisateur de bout en bout, donc PAS besoin du hack XAUTHORITY/sudo utilisé
par les outils standalone (`_fix_x11_env_for_sudo` dans spi_flash_gui.py /
clone_odroid_gui.py) — celui-ci reste pertinent seulement pour l'usage manuel/dev
(`sudo python3 spi_flash_gui.py` depuis une session X déjà ouverte).

Lancer manuellement (dev/debug, avec un serveur X déjà disponible) :
    sudo python3 station_gui.py
Échap bascule le plein écran (utile en debug via SSH -X ou VNC).
"""

import os
import sys
import tkinter as tk
from tkinter import ttk

from spi_flash_gui import SpiPanel
from clone_odroid_gui import ClonePanel
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
        notebook.add(ClonePanel(notebook), text="Clone NVMe")
        notebook.add(VerifyPanel(notebook), text="Vérification")

    def _toggle_fullscreen(self, _event=None):
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)


if __name__ == "__main__":
    if os.name != "posix":
        print("Outil Linux (poste de déploiement Odroid-M1).")
        sys.exit(1)
    if os.geteuid() != 0:
        print("⚠ Pas lancé en root : flash SPI, clonage et fw_setenv échoueront. "
              "Relance avec : sudo python3 station_gui.py")

    try:
        app = StationGUI()
    except tk.TclError as e:
        print(f"Impossible d'ouvrir la fenêtre graphique : {e}")
        print("Sur le poste kiosque, ce script est lancé par systemd/X — voir "
              "journalctl -u odroid-station -f. En dev, vérifie DISPLAY/X11.")
        sys.exit(1)

    app.mainloop()
