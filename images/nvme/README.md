# Image disque NVMe — Odroid-M1 (à venir)

Emplacement prévu pour l'**image complète du disque NVMe** de production (partition
BOOT ext2 avec `boot.scr`/`cma=128M` + rootfs ext4), à distribuer avec le paquet dans
le futur repo séparé.

Deux usages complémentaires (voir [`../../docs/DEPLOIEMENT_FLOTTE.md`](../../docs/DEPLOIEMENT_FLOTTE.md)) :

- **Clonage vers un NVMe neuf** avec `odroid-clone` (mode boot « SPI ») : identité
  régénérée, `boot.scr`/fstab/initramfs réécrits, racine étendue à la taille cible.
- **Restauration bit-à-bit** (`dd` / image brute) si l'on veut un clone strictement
  identique.

> Le NVMe ne porte **pas** le bootloader : celui-ci vit dans la puce **SPI**
> (`../spi/golden_spi_16MiB.bin`). Une carte n'est bootable qu'avec **les deux**
> (SPI flashée + disque écrit).

Fichiers attendus ici (non encore présents) :

| Fichier | Rôle |
|---------|------|
| `odroid_m1_nvme.img` (ou `.img.zst`) | image disque complète |
| `odroid_m1_nvme.img.sha256` | empreinte de contrôle |

Une image disque est volumineuse : prévoir Git LFS ou un stockage d'artefacts dans
le repo séparé (le golden SPI, lui, ne fait que 16 MiO et reste un blob git normal).
