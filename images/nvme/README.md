# Image disque NVMe — Odroid-M1

Sauvegarde du NVMe de production (partition BOOT avec `boot.scr`/`cma=128M` +
rootfs ext4), à restaurer sur un NVMe neuf. Deux formats acceptés par l'outil :

### A. Sauvegarde partclone (bundle) — *présent ici*

Sauvegarde type Clonezilla : la table de partitions + une image **partclone**
par partition (blocs utilisés seulement). Fichiers du bundle :

| Fichier | Rôle |
|---------|------|
| `sda-partition-table.sfdisk` | table de partitions (dos : BOOT 512 Mio + rootfs) |
| `sda1-BOOT.pc` | image partclone de la partition BOOT (ext) |
| `sda2-rootfs.pc` | image partclone du rootfs (ext, blocs utilisés → ~15 Go) |
| `sda-first1M.bin` | 1er Mio brut (table MBR ; secteur 64 vide en boot-SPI) |

Restauration sur un disque vierge (le **dossier** entier passe en `--image`) :
```bash
sudo odroid-station clone --image images/nvme --dest /dev/nvme0n1
```
L'outil écrit une table à identité NEUVE (racine étendue à la cible),
`partclone.restore` chaque partition, agrandit le rootfs (`resize2fs`), régénère
les UUID (`tune2fs -U random`) puis réécrit `fstab`/`boot.scr` (CRC recalculés)
et reconstruit l'initramfs — comme un clone disque. Nécessite le paquet
`partclone` (installé par `install.sh`). En GUI : onglet Clone → source
« Sauvegarde partclone (dossier) ».

### B. Image compacte `.img` (alternative)

Produite par l'outil, taillée sur l'espace UTILISÉ (sparse) :
```bash
# master ÉTEINT, NVMe branché en USB (clone à froid) :
sudo odroid-station image --source /dev/sdX --out images/nvme/odroid_m1_nvme.img
sudo odroid-station clone --image images/nvme/odroid_m1_nvme.img --dest /dev/nvme0n1
```

> Le NVMe ne porte **pas** le bootloader : celui-ci vit dans la puce **SPI**
> (`../spi/golden_spi_16MiB.bin`). Une carte n'est bootable qu'avec **les deux**
> (SPI flashée + disque restauré). Voir le runbook :
> [`../../docs/DEPLOIEMENT_FLOTTE.md`](../../docs/DEPLOIEMENT_FLOTTE.md).

Une sauvegarde disque reste volumineuse (~15 Go ici) : prévoir Git LFS ou un
stockage d'artefacts (le golden SPI, lui, fait 16 MiO et reste un blob git
normal). Les `.pc` partclone sont déjà compacts ; pour distribuer, compresser
en `.zst` si besoin.
