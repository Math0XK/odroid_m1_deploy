# Déploiement de flotte Odroid-M1 — boot NVMe direct via SPI

Runbook reproductible pour dupliquer une carte Odroid-M1 de production (RK3568) :
sauvegarde du bootloader SPI « golden », flash de la flotte, clonage du NVMe,
vérification post-déploiement. Remplace le single-point-of-failure où tout reposait
sur un unique master physique.

Décisions de fond : [DECISIONS.md](../../docs/DECISIONS.md) **D33/D34** (clonage) et
**D49** (golden SPI + flash flotte). Détail technique d'origine :
[annexe de passation](odroid-m1-boot-nvme-npu-handoff.md).

---

## 1. Architecture de boot (à comprendre avant tout)

```
Power on
 → BootROM RK3568 (cherche la SPI en premier)
 → Puce SPI 16 MiO (5 partitions MTD) :
     mtd0 SPL         = idbloader-spi.img (Armbian)
     mtd1 U-Boot Env  = env U-Boot (persiste via fw_setenv) ← 4 vars critiques
     mtd2 U-Boot      = u-boot 2026.01 Armbian
     mtd3 splash / mtd4 Filesystem
 → u-boot exécute distro_bootcmd, boot_targets = "nvme mmc1 ..."
 → cible NVMe : active le régulateur NPU (vdd_npu), pci enum, nvme scan,
     lit /boot.scr sur nvme0n1p1 (BOOT, ext2) → charge kernel/initrd/dtb
     avec cma=128M → root sur nvme0n1p2 (ext4)
 → si le NVMe échoue → fallback propre sur mmc1 (SD Armbian)
```

**Points clés :**
- Le **bootloader vit dans la SPI**, pas sur le disque. Le NVMe boote **seul** — plus
  de « support de boot séparé », plus de petitboot.
- Le NVMe master porte peut-être un **idbloader résiduel** au secteur 64 (vestige
  d'un ancien clone) : **sans effet**, le boot passe par la SPI. Ne jamais s'y fier.
- 3 correctifs indispensables au NPU/boot (voir §7) : régulateur `vdd_npu` activé,
  `pci enum` avant `nvme scan`, `cma=128M` dans le `boot.scr`.

---

## 2. Le poste de déploiement (installation, une seule fois)

Le paquet est conçu pour un **poste dédié** : image Ubuntu/Armbian fraîche et
**minimale** (sans bureau graphique — comme l'image ODROID-M1 vendor), sur laquelle
il installe tout et se lance automatiquement au boot.

```bash
git clone <url-du-repo>          # une fois le repo séparé créé
cd .../odroid_m1_deploy
sudo ./install.sh
sudo reboot
```

`install.sh` installe **toutes** les dépendances (flashrom, u-boot-tools, clonage,
X minimal sans gestionnaire de bureau) et un **service systemd**
(`odroid-station`, cible `graphical.target`) qui lance **l'outil unique**
(`station.py`, plein écran, 3 onglets : SPI / Clone-Image / Vérification) dès le
démarrage suivant — aucun geste manuel après le premier boot. Idempotent
(relançable après un `git pull`).

```bash
journalctl -u odroid-station -f      # déboguer le service
sudo systemctl disable odroid-station  # désactiver le boot kiosque si besoin
```

Détail : [`../README.md`](../README.md). Les étapes ci-dessous (3 à 8) référencent
les **onglets** du tableau de bord (`sudo odroid-station`) ; **le même outil** en
SSH sans X11 expose chaque action en sous-commande (`odroid-station clone|image|
spi|check`, voir `odroid-station --help`) — même code, mêmes garde-fous.

| Poste (si pas d'install dédiée) | Outils |
|-------|--------|
| Banc (pince) | Programmeur **CH341A** + pince **SOIC-8**, `flashrom`, Python3 + tkinter |
| Unité (on-device) | `flashrom`, `u-boot-tools` (`fw_setenv`/`fw_printenv`), `flash-kernel`, `mkimage` |
| PC de clonage | Linux (ou WSL2), `sfdisk`/`blkid`/`rsync`/`parted`/`mkfs.*`, Python3 + tkinter |

> Tous les outils SPI/clone sont **Linux**. La logique pure est testée à part :
> `python -m pytest tests/test_spi_core.py tests/test_clone_core.py
> tests/test_clone_uboot.py`.

---

## 3. Étape 1 — Sauvegarder le golden depuis le master (pince)

Sur le **master** (celui qui boote correctement), **hors tension**, pince SOIC-8 sur
la puce SPI :

```bash
sudo odroid-station            # onglet "SPI (Golden / Flash / Env)"
# ou en SSH : sudo odroid-station spi read
```
Programmer = **« Pince CH341A »** → **« Lire la puce → golden »**. L'outil :
1. lit les 16 MiO (`flashrom -p ch341a_spi -r …`) ;
2. valide (taille exacte, image non vierge, bannière `U-Boot`) — refuse sinon ;
3. écrit `images/spi/golden_spi_16MiB.bin` + `.sha256` ;
4. avertit si l'env contient une `ethaddr` figée (voir §4).

Puis **committer** le golden (source de vérité versionnée) :
```bash
git add images/spi/golden_spi_16MiB.bin images/spi/golden_spi_16MiB.bin.sha256
git commit -m "Golden SPI Odroid-M1 (master <date>)"
```

Sauvegarder aussi l'env lisible depuis le master **on-device** :
bouton **« Sauver l'env (fw_printenv) »** (ou `sudo fw_printenv`).

---

## 4. Étape 2 — Contrôles sur le master AVANT de flasher la flotte

Deux vérifs à faire une fois, sur le master, pour ne pas propager un défaut :

**a) Divergence UUID du `boot.scr`.** La racine y est désignée par un UUID qui doit
correspondre à la vraie partition NVMe :
```bash
sudo strings /boot/boot.scr | grep -oE 'root=(UUID|PARTUUID)=[0-9a-fA-F-]+'
lsblk -o NAME,UUID,PARTUUID /dev/nvme0n1     # comparer
```
Si l'UUID du `boot.scr` **diffère** de celui de `nvme0n1p2` (cas connu :
`eee2b90d…` vs `a9bdb4f9…`), le master boote peut-être « par coïncidence ».
Le moteur de clonage rend ça inoffensif pour le **clone** (il remappe cet
identifiant vers l'identité neuve, cf. `extract_root_ids`), mais clarifier l'origine
sur le master évite de perpétuer une config bancale.

**b) MAC figée (`ethaddr`).** Flasher l'**image complète** propage l'env, donc une
`ethaddr` sauvegardée → **même MAC sur toute la flotte** :
```bash
sudo fw_printenv ethaddr     # si défini => collision potentielle
```
Le RK3568 dérive normalement sa MAC de l'efuse ; si `ethaddr` est présent dans l'env,
le purger sur le master avant de régénérer le golden (`sudo fw_setenv ethaddr` sans
valeur), **ou** flasher par-partition (mtd0/mtd2 seuls) + `fw_setenv` par unité (§6).

---

## 5. Étape 3 — Flasher la SPI d'une unité

`sudo odroid-station` (onglet SPI) → **« Flasher cette unité avec le golden »** —
ou en SSH : `sudo odroid-station spi flash`. L'outil sauvegarde d'abord la puce
cible (`preflash_backups/`), contrôle le golden (taille + signature + SHA256 ==
manifeste), demande confirmation, écrit et vérifie.

- **Pince CH341A** (seule méthode) : unité **hors tension**, pince SOIC-8 sur la
  puce, marche même sur une carte vierge/briquée.
- Le flash/lecture **on-device** (« à chaud », `internal`) n'est **pas supporté** :
  le contrôleur SPI du RK3568 (SFC) n'expose pas la puce entière à Linux
  (`flashrom -p internal` n'existe pas sur le flashrom ARM64 ; les partitions MTD
  ne couvrent pas les 16 MiO). Toujours passer par la pince, carte hors tension.

> **Case « Mode simulation »** : journalise les commandes `flashrom` exactes sans
> rien exécuter — à utiliser pour relire un enchaînement avant de le lancer pour de
> vrai.

Équivalent manuel (pince) :
```bash
sudo flashrom -p ch341a_spi -r preflash_backup.bin          # filet
sudo flashrom -p ch341a_spi -w images/spi/golden_spi_16MiB.bin
```

---

## 6. Étape 4 — (Voie par-partition) ré-appliquer les 4 vars d'env

Uniquement si l'env a été remis à l'état d'origine (flash de mtd0/mtd2 seuls, ou
`armbian-install` sur une carte neuve). Sur l'unité, en root :

`sudo odroid-station` (onglet SPI) → **« Appliquer les 4 vars d'env »**, ou
`spi_core.fw_setenv_commands()`, ou manuellement
(voir [`images/spi/uboot_env.txt`](../images/spi/uboot_env.txt)) :
```bash
sudo fw_setenv boot_targets 'nvme mmc1 mmc0 mtd2 mtd1 mtd0 usb0 pxe dhcp'
sudo fw_setenv npu_regulator_enable 'regulator dev vdd_npu; regulator value 900000; regulator enable'
sudo fw_setenv nvme_boot 'run npu_regulator_enable; pci enum; nvme scan; if nvme device ${devnum}; then setenv devtype nvme; run scan_dev_for_boot_part; fi'
sudo fw_setenv bootcmd_nvme 'setenv devnum 0; run nvme_boot'
```
> Un **flash d'image complète** (§5) embarque déjà mtd1 : cette étape est alors inutile.

---

## 7. Étape 5 — Cloner le NVMe vers l'unité (ou en faire une image compacte)

Depuis ce poste ou un **PC Linux** (clone à FROID : NVMe cible branché en USB,
jamais le disque système) :
```bash
sudo odroid-station            # onglet "Clone / Image"
# ou en SSH :
sudo odroid-station clone --source /dev/sda --dest /dev/nvme0n1
sudo odroid-station clone --image images/nvme/odroid_m1_nvme.img --dest /dev/nvme0n1
```
- **Mode de boot = « SPI »** (défaut) : le clone reçoit une identité neuve
  (UUID/PARTUUID), fstab + `boot.scr` (CRC recalculés, `cma=128M` préservé) + initramfs
  réécrits ; la zone bootloader du disque est ignorée (le boot vient de la SPI).
- Un idbloader résiduel sur la source est signalé **sans effet**.
- Reconstruire l'initramfs **sur l'ODROID** (ARM64) si le clone a tourné sur un PC x86.

Le clone ne bootera que sur une carte dont la **SPI porte le golden** (§5).

**Image disque compacte (source de clonage).** Destination « Fichier image
compact » dans l'onglet, ou en SSH :
```bash
sudo odroid-station image --source /dev/sda --out images/nvme/odroid_m1_nvme.img
```
L'image est **taillée sur l'espace UTILISÉ** de la racine (pas la capacité du
disque : un NVMe 128 Go rempli à 20 % → ~30 Go) et **sparse**. Au clonage depuis
l'image, la racine est ré-étendue à la taille de la cible.

**Clone à FROID uniquement.** Le clonage et l'imagerie lisent toujours un disque
source **éteint** (l'Odroid source hors tension, sa carte/eMMC/NVMe branchée en
lecteur USB) ou un fichier image — jamais le disque système de la machine en
marche. Le disque système est **refusé en source comme en destination**, quoi
qu'il arrive : c'est le chemin fiable pour un master de flotte (instantané figé,
remonté read-only).

---

## 8. Étape 6 — Vérification post-déploiement (GO/NO-GO)

**Sur l'unité** flashée + clonée — onglet **« Vérification »** du tableau de bord
(`sudo odroid-station`), ou en headless (SSH, flash fait depuis un autre poste) :
```bash
sudo odroid-station check
# avec smoke test NPU (infer_rknn.py fait partie de l'install Harvest sur l'unité) :
sudo odroid-station check \
    --npu-cmd "python3 infer_rknn.py --model best_rknn_model/best.rknn --benchmark --runs 20"
```

Contrôles : `uname -r == 5.10.0-odroid-arm64`, racine sur NVMe, **dmesg sans**
`failed to get ack` / `failed to allocate`, `/dev/dri/card0` + `renderD128` présents,
inférence NPU < 100 ms. Tous OK → **GO**.

---

## 9. Pièges connus (issus de la mise au point)

- **`boot.scr` : ne jamais round-tripper via `dumpimage`.** `dumpimage … -o x.cmd` →
  `sed` → `mkimage … x.cmd boot.scr` produit un script qui échoue silencieusement
  (`SCRIPT FAILED`). **Toujours écrire le `.cmd` à la main (heredoc), puis compiler :**
  ```bash
  cat > /tmp/boot.cmd <<'EOF'
  # … contenu connu du bootscr flash-kernel, avec cma=128M dans les bootargs …
  EOF
  mkimage -C none -A arm64 -T script -d /tmp/boot.cmd /boot/boot.scr
  ```
- **Kernel panic « failed to get ack on domain 'npu' »** : régulateur `vdd_npu`
  (DCDC_REG4 du RK809) non activé → `npu_regulator_enable` (§6).
- **« cma_alloc … failed » / « RKNPU: failed to allocate »** : CMA par défaut (16 Mio)
  insuffisant → `cma=128M` dans le `boot.scr` (préservé par le clonage).
- **`Device 0: unknown device` au `nvme scan`** : lien PCIe pas entraîné → `pci enum`
  avant `nvme scan` (déjà dans `nvme_boot`).

---

## 10. Déploiement sur un Odroid-M1 + NVMe NEUFS (sans golden)

Si l'on ne part pas d'un flash de golden :
1. Flasher la SPI Armbian : image Hardkernel Ubuntu 22.04.5 + `armbian-install`
   **option 7** (flash SPI — **PAS** l'option 4 qui installe un rootfs Armbian).
2. Appliquer les 4 `fw_setenv` (§6).
3. Ajouter `cma=128M` au `boot.scr` du NVMe (méthode heredoc, §9).
4. `sudo odroid-station check` (§8).

Puis, pour les suivantes, préférer le **golden** (§3-§5) : plus rapide et fidèle.

---

## Annexe

Note de passation d'origine (root causes détaillées, état du système à la mise au
point) : [`odroid-m1-boot-nvme-npu-handoff.md`](odroid-m1-boot-nvme-npu-handoff.md).
