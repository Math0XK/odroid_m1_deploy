# odroid_m1_deploy — poste de déploiement de flotte Odroid-M1

**Un seul outil** (`odroid-station`) pour dupliquer une carte Odroid-M1 de
production (RK3568) : sauvegarde/flash du bootloader **SPI** (à la pince
CH341A), clonage du **NVMe** à froid, création d'**images disque compactes**
(source de clonage), et vérification post-déploiement — en graphique **et** en
ligne de commande (SSH). Conçu comme un **poste dédié** :
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
manager), crée le lanceur unique `odroid-station`, et installe/active le
service systemd `odroid-station` (cible `graphical.target`) qui lance l'outil
**plein écran** dès le prochain démarrage — aucun geste manuel après le premier
boot. Idempotent : relancer la même ligne met à jour le checkout (`git pull`)
et réinstalle par-dessus.

Depuis un checkout local, l'équivalent classique marche aussi :

```bash
git clone https://github.com/Math0XK/odroid_m1_deploy.git
cd odroid_m1_deploy
sudo ./install.sh
```

## Désinstallation

```bash
cd /opt/odroid_m1_deploy   # ou le checkout local utilisé pour l'install
sudo ./uninstall.sh                   # service kiosque + cible de boot + lanceur
sudo ./uninstall.sh --purge-repo      # + checkout /opt/odroid_m1_deploy
sudo ./uninstall.sh --purge-packages  # + paquets apt spécifiques au kiosque
sudo ./uninstall.sh --all             # tout
sudo reboot
```

Par défaut, ne touche pas aux paquets apt (plusieurs sont des paquets système
partagés : `util-linux`, `python3`, `parted`, `rsync`, `dosfstools`,
`e2fsprogs`) ni au checkout du repo — à demander explicitement via
`--purge-packages`/`--purge-repo`/`--all`.

## L'outil unique : `odroid-station`

Sans argument : **tableau de bord graphique** plein écran, 3 onglets (identique
au lancement automatique du boot kiosque). `Échap`/`F11` bascule le plein écran.

```bash
sudo odroid-station
```

Avec une sous-commande : les **mêmes opérations en CLI headless** (SSH sans
X11), mêmes moteurs, mêmes garde-fous :

```bash
sudo odroid-station list                                        # disques détectés
sudo odroid-station clone --source /dev/sda --dest /dev/nvme0n1 # clonage à froid
sudo odroid-station clone --image master.img --dest /dev/nvme0n1        # image .img
sudo odroid-station clone --image images/nvme --dest /dev/nvme0n1       # bundle partclone (dossier)
sudo odroid-station image --source /dev/sda --out master.img    # image COMPACTE
sudo odroid-station spi read                                    # puce SPI -> golden (pince)
sudo odroid-station spi flash                                   # golden -> puce (pince)
sudo odroid-station spi verify | env-apply | env-save
sudo odroid-station check                                       # GO/NO-GO sur l'unité
```

Points notables :

- **Audit de boot automatique** : chaque clonage/restauration se termine par un
  GO/NO-GO vérifié SUR le clone (CRC du `boot.scr`, `root=` résolu vers un UUID
  qui existe vraiment, `cma=128M` présent, fstab cohérent, kernel+initrd
  présents, configs flash-kernel assainies, features ext compatibles avec le
  noyau 5.10 de l'unité). Un NO-GO fait **échouer** l'opération : un disque qui
  ne trouverait pas sa racine au boot n'est jamais déclaré réussi.
- **Journal structuré** : étapes numérotées (« ÉTAPE 3/10 — Formatage… »),
  succès en vert, avertissements en orange, erreurs en rouge, récapitulatif
  final — dans la GUI (bandeau d'état + journal en couleurs) comme en CLI
  (préfixes ✔/⚠/✖, couleurs ANSI).
- **Image compacte** : `image` (ou destination « Fichier image compact » dans
  l'onglet Clone / Image) produit une image **taillée sur l'espace UTILISÉ** de
  la racine, pas sur la capacité du disque — un NVMe 128 Go rempli à 20 % donne
  ~30 Go, en fichier **sparse**. Au clonage depuis l'image, la racine est
  ré-étendue à la taille de la cible.
- **Restauration partclone** : `clone --image <DOSSIER>` (ou source « Sauvegarde
  partclone » dans l'onglet) restaure une sauvegarde type Clonezilla (table
  `.sfdisk` + une image `.pc` par partition) sur un disque vierge, avec la même
  identité neuve, réécriture `fstab`/`boot.scr` et initramfs que le clonage
  disque. Nécessite le paquet `partclone`.
- **Clone à FROID uniquement** : la source est un Odroid **éteint** dont la
  carte/eMMC/NVMe est branchée en lecteur USB (ou un fichier image). Le disque
  **système** de la machine en marche est **refusé** en source comme en
  destination — pas d'auto-clonage à chaud.
- **SPI hors de Linux** : le flash SPI ne se fait pas depuis Linux (le SFC du
  RK3568 n'expose pas la puce entière — `flashrom -p internal` inexistant, MTD
  partitionné). Deux voies : **(1) prompt U-Boot `sf`** (SANS pince, recommandé —
  `sf read`/`sf write` voient la puce brute 16 Mio ; golden sur clé USB) ; **(2)
  pince CH341A** carte hors tension (`odroid-station spi read|flash`, `ch341a_spi`).
  Détail des commandes : [`docs/DEPLOIEMENT_FLOTTE.md`](docs/DEPLOIEMENT_FLOTTE.md) §3/§5.
- `--sim` (ou case « Mode simulation ») : journalise les commandes exactes sans
  rien exécuter.

Débogage du service kiosque : `journalctl -u odroid-station -f`.

## Structure

```
odroid_m1_deploy/
  install.sh                installeur Linux une-ligne : bootstrap git + deps +
                             lanceur unique + boot kiosque
  uninstall.sh               défait install.sh : service kiosque, cible de
                             boot, lanceur (+ paquets/checkout en option)
  tools/
    station.py              L'OUTIL (entrée unique) : GUI plein écran sans
                             argument, CLI en sous-commandes (clone/image/spi/check)
    spi_core.py             logique pure SPI + parsers de vérif (testée)
    spi_ops.py              opérations SPI réelles (flashrom/fw_setenv), partagées
                             GUI/CLI : garde-fous, backup pré-flash, SHA256
    spi_panel.py            onglet SPI (golden / flash flotte / env)
    clone_core.py           logique pure du clonage (testée)
    clone_engine.py         MOTEUR clonage à froid + image compacte + restore
                             partclone (sans UI), audit de boot intégré
    boot_audit.py           audit de bootabilité du clone (GO/NO-GO, testé)
    report.py               journal structuré partagé GUI/CLI (niveaux, étapes)
    ui_widgets.py           briques GUI communes (bandeau d'état, journal couleurs)
    clone_panel.py          onglet Clone / Image
    verify_panel.py         onglet Vérification
    check_deploy.py         contrôles GO/NO-GO (module partagé onglet/CLI)
  station/
    odroid-station.service  unité systemd (boot kiosque, template @REPO_ROOT@)
    xinitrc                 lance X minimal (sans gestionnaire de fenêtres)
  images/
    spi/                    golden SPI 16 MiO (versionné) + SHA256 + env de référence
    nvme/                   image disque NVMe compacte (à produire — voir son README)
  docs/
    DEPLOIEMENT_FLOTTE.md   runbook complet (lecture golden → flash → clone → vérif)
    odroid-m1-boot-nvme-npu-handoff.md  note de passation (root causes détaillées)
  tests/                    tests pure-logique (sans matériel)
```

GUI et CLI pilotent **les mêmes moteurs** (`clone_engine.CloneEngine`,
`spi_ops.SpiOps`) — pas de duplication, mêmes garde-fous partout (refus du
disque système en source comme en destination, clone à froid uniquement,
identité neuve du clone, backup pré-flash de la puce).

## Démarrage rapide (dans le tableau de bord)

1. **Onglet SPI** : sur le master, à la pince CH341A → « Lire la puce → golden »
   → committer `images/spi/golden_spi_16MiB.bin`.
2. **Onglet SPI** : sur chaque unité, à la pince CH341A → « Flasher cette unité ».
3. **Onglet Clone / Image** : cloner vers le NVMe cible (mode « SPI »), ou créer
   une image compacte comme source réutilisable. En SSH :
   `sudo odroid-station clone --source /dev/sdX --dest /dev/nvme0n1`.
4. **Onglet Vérification** : sur l'unité → GO/NO-GO (`sudo odroid-station check`).

Détail, pièges et déploiement d'une carte neuve : [`docs/DEPLOIEMENT_FLOTTE.md`](docs/DEPLOIEMENT_FLOTTE.md).

## Tests (sans matériel)

```bash
python3 -m pytest tests/ -q
```
