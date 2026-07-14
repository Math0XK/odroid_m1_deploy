# Golden SPI — Odroid-M1 (RK3568)

Sauvegarde **durable et versionnée** de la configuration de boot SPI de l'Odroid-M1,
pour sortir du single-point-of-failure (tout reposait sur un unique master physique).

Voir le runbook complet : [`../../docs/DEPLOIEMENT_FLOTTE.md`](../../docs/DEPLOIEMENT_FLOTTE.md).

## Contenu

| Fichier | Rôle | Versionné ? |
|---------|------|-------------|
| `golden_spi_16MiB.bin` | Image brute 16 MiO de la puce SPI du master (idbloader-spi + u-boot 2026.01 Armbian + env U-Boot). | **Oui** (source de vérité) |
| `golden_spi_16MiB.bin.sha256` | Empreinte SHA256 du golden, vérifiée avant chaque flash. | **Oui** |
| `uboot_env.txt` | Les 4 variables d'env U-Boot critiques (référence lisible + `fw_setenv`). | **Oui** |
| `preflash_backups/` | Dumps de la puce de chaque unité, faits **avant** son flash (filet anti-brique). | Non (`.gitignore`) |
| `uboot_env_<host>_<ts>.txt` | Dumps `fw_printenv` horodatés. | Non (`.gitignore`) |

> Le `.bin` est marqué `binary` dans `.gitattributes` (aucune conversion de fin de
> ligne : un CRLF injecté corromprait le bootloader). 16 MiO tient sans souci en blob
> git ordinaire (pas de LFS).

## Régénérer le golden

**Sans pince — prompt U-Boot `sf`** (recommandé) : sur le master, lire la puce
brute vers une clé USB, puis renommer/hasher :
```text
=> sf probe
=> sf read ${kernel_addr_r} 0x0 0x1000000
=> usb start
=> fatwrite usb 0:1 ${kernel_addr_r} fulldump_spi.bin 0x1000000
```
```bash
mv fulldump_spi.bin golden_spi_16MiB.bin
sha256sum golden_spi_16MiB.bin | awk '{print $1"  golden_spi_16MiB.bin"}' \
    > golden_spi_16MiB.bin.sha256
```
Le dump est valide s'il fait 16 MiO, commence par `RKNS` (idbloader) et contient
la bannière `U-Boot`. Vérifie aussi la divergence UUID / une `ethaddr` figée
(voir runbook) avant de flasher la flotte.

**À la pince CH341A** (master hors tension) — l'outil valide + hashe tout seul :
```bash
sudo odroid-station            # onglet SPI, « Pince CH341A » → « Lire → golden »
sudo odroid-station spi read   # équivalent SSH sans X11
```

## Flasher une unité

**Sans pince — prompt U-Boot `sf`** (golden sur clé USB) :
```text
=> sf probe
=> usb start
=> fatload usb 0:1 ${kernel_addr_r} golden_spi_16MiB.bin
=> sf erase 0x0 0x1000000
=> sf write ${kernel_addr_r} 0x0 0x1000000
```

**À la pince CH341A** (unité hors tension ; backup pré-flash auto) :
```bash
sudo odroid-station            # onglet SPI → « Flasher cette unité avec le golden »
sudo odroid-station spi flash  # équivalent SSH
```

Puis vérifier **sur l'unité** : `sudo odroid-station check`.
