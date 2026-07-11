# odroid_m1_deploy — poste de déploiement de flotte Odroid-M1

Outil **autonome** de duplication d'une carte Odroid-M1 de production (RK3568) :
sauvegarde/flash du bootloader **SPI**, clonage du **NVMe** (GUI **et** CLI
headless), et vérification post-déploiement. Conçu comme un **poste dédié** :
sur une image Ubuntu/Armbian fraîche et minimale (sans bureau graphique), **une
seule ligne de commande** installe tout — le tableau de bord se lance ensuite
automatiquement, plein écran, à chaque démarrage (service systemd, boot
kiosque).

> Extrait du repo [harvest_project](https://github.com/Math0XK/harvest_project)
> (07/2026) — l'outillage de déploiement de flotte vit désormais ici, avec les
> images (golden SPI + image disque NVMe). Le contexte produit (piège Harvest,
> décisions D33/D34/D49) reste documenté là-bas.

## Installation en une ligne (poste dédié, image fraîche)

```bash
curl -fsSL https://raw.githubusercontent.com/Math0XK/odroid_m1_deploy/main/install.sh | sudo sh
sudo reboot
```

Le script se bootstrappe tout seul : il installe `git`, clone ce repo dans
`/opt/odroid_m1_deploy`, installe **tout** (flashrom, u-boot-tools, clonage
sfdisk/rsync/…, X minimal `xserver-xorg`/`xinit` — **pas** de GDM/LightDM/window
manager), crée les lanceurs, et installe/active le service systemd
`odroid-station` (cible `graphical.target`) qui lance `station_gui.py` **plein
écran** dès le prochain démarrage — aucun geste manuel après le premier boot.
Idempotent : relancer la même ligne met à jour le checkout (`git pull`) et
réinstalle par-dessus.

Depuis un checkout local, l'équivalent classique marche aussi :

```bash
git clone https://github.com/Math0XK/odroid_m1_deploy.git
cd odroid_m1_deploy
sudo ./install.sh
```

Lanceurs créés dans `/usr/local/bin` (utiles aussi pour du debug manuel sur le
poste kiosque) :

```bash
sudo odroid-station          # tableau de bord unifié (identique au boot auto)
sudo odroid-spi-flash        # onglet SPI seul (golden / flash flotte / env)
sudo odroid-clone            # onglet Clone seul (clonage NVMe, GUI)
sudo odroid-clone-cli        # clonage NVMe HEADLESS (SSH sans X11) : --help
sudo odroid-check-deploy     # vérif post-déploiement GO/NO-GO (CLI headless SSH)
```

Débogage du service : `journalctl -u odroid-station -f`. `Échap`/`F11` dans l'app
bascule le plein écran (utile en debug via SSH -X / VNC).

## Structure

```
odroid_m1_deploy/
  install.sh                   installeur Linux une-ligne : bootstrap git +
                                deps + lanceurs + boot kiosque
  tools/
    station_gui.py             POINT D'ENTRÉE du poste : tableau de bord unifié
                                (Notebook 3 onglets, plein écran)
    spi_core.py                logique pure SPI + parsers de vérif (testée)
    spi_flash_gui.py           SpiPanel (onglet SPI) + lançable seul (dev)
    clone_core.py              logique pure du clonage (testée)
    clone_engine.py            MOTEUR du clonage (sans UI) — partagé GUI/CLI
    clone_odroid_gui.py        ClonePanel (onglet Clone) + lançable seul (dev)
    clone_cli.py               clonage headless (SSH sans X11) : --list/--source/
                                --dest/--boot-mode/--yes
    verify_panel.py            VerifyPanel (onglet Vérif) + lançable seul (dev)
    check_deploy.py            logique de vérif GO/NO-GO (CLI headless + panel)
  station/
    odroid-station.service     unité systemd (boot kiosque, template @REPO_ROOT@)
    xinitrc                    lance X minimal (sans gestionnaire de fenêtres)
  images/
    spi/                       golden SPI 16 MiO (versionné) + SHA256 + env de référence
    nvme/                      image disque NVMe complète (à ajouter — voir son README)
  docs/
    DEPLOIEMENT_FLOTTE.md      runbook complet (lecture golden → flash → clone → vérif)
    odroid-m1-boot-nvme-npu-handoff.md  note de passation (root causes détaillées)
  tests/                       tests pure-logique (sans matériel)
```

`station_gui.py` réutilise **le même code** que les 3 outils individuels (`SpiPanel`,
`ClonePanel`, `VerifyPanel` sont des `ttk.Frame` embarquables), et la GUI comme le
CLI de clonage pilotent **le même moteur** (`clone_engine.CloneEngine`) — pas de
duplication, mêmes garde-fous partout (refus du disque système, clone à froid,
identité neuve).

## Démarrage rapide (dans le tableau de bord)

1. **Onglet SPI** : sur le master, à la pince CH341A → « Lire le master → golden »
   → committer `images/spi/golden_spi_16MiB.bin`.
2. **Onglet SPI** : sur chaque unité (pince ou on-device) → « Flasher cette unité ».
3. **Onglet Clone** : depuis ce poste (NVMe cible branché en USB) → mode « SPI ».
   En SSH sans écran : `sudo odroid-clone-cli --source /dev/sdX --dest /dev/nvme0n1`.
4. **Onglet Vérification** : sur l'unité → GO/NO-GO (`sudo odroid-check-deploy` en SSH).

Détail, pièges et déploiement d'une carte neuve : [`docs/DEPLOIEMENT_FLOTTE.md`](docs/DEPLOIEMENT_FLOTTE.md).

## Tests (sans matériel)

```bash
python3 -m pytest tests/ -q
```
