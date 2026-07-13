# Image disque NVMe — Odroid-M1 (à produire)

Emplacement prévu pour l'**image disque compacte** du NVMe de production
(partition BOOT avec `boot.scr`/`cma=128M` + rootfs ext4). À produire avec
l'outil lui-même — l'image est **taillée sur l'espace UTILISÉ** de la racine
(pas la capacité du disque : un NVMe 128 Go rempli à 20 % → ~30 Go) et
**sparse** :

```bash
# depuis le poste, disque master branché en USB (clone à froid) :
sudo odroid-station image --source /dev/sdX --out images/nvme/odroid_m1_nvme.img
# ou depuis l'Odroid master lui-même, EN MARCHE (services arrêtés) :
sudo odroid-station image --source /dev/nvme0n1 --out /media/usb/odroid_m1_nvme.img --live
```

Usage principal (voir [`../../docs/DEPLOIEMENT_FLOTTE.md`](../../docs/DEPLOIEMENT_FLOTTE.md)) :
**clonage vers un NVMe neuf** avec `odroid-station clone --image … --dest …`
(mode boot « SPI ») : identité régénérée, `boot.scr`/fstab/initramfs réécrits,
racine ré-étendue à la taille de la cible.

> Le NVMe ne porte **pas** le bootloader : celui-ci vit dans la puce **SPI**
> (`../spi/golden_spi_16MiB.bin`). Une carte n'est bootable qu'avec **les deux**
> (SPI flashée + disque écrit).

Fichiers attendus ici (non encore présents) :

| Fichier | Rôle |
|---------|------|
| `odroid_m1_nvme.img` (ou `.img.zst`) | image disque compacte |
| `odroid_m1_nvme.img.sha256` | empreinte de contrôle |

Une image disque reste volumineuse : prévoir Git LFS ou un stockage d'artefacts
(le golden SPI, lui, ne fait que 16 MiO et reste un blob git normal). Le fichier
est **sparse** : le copier avec `cp --sparse=always` / `rsync -S`, ou le
compresser (`zstd odroid_m1_nvme.img`) pour le distribuer.
