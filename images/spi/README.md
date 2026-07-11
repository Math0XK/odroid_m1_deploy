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

Sur le **master** (celui qui boote déjà correctement), à la **pince CH341A** (carte
hors tension) :

```bash
sudo odroid-spi-flash          # (ou : sudo python3 ../../tools/spi_flash_gui.py)
# Programmer = « Pince CH341A » → bouton « Lire le master → golden »
```

L'outil valide le dump (16 MiO, non vierge, bannière « U-Boot ») avant de l'écrire
ici, puis calcule le `.sha256`. **Ne commiter que si la validation passe.** Vérifie
aussi la divergence UUID et une éventuelle `ethaddr` figée (voir runbook) avant de
flasher la flotte.

## Flasher une unité

```bash
sudo odroid-spi-flash          # (ou : sudo python3 ../../tools/spi_flash_gui.py)
# Choisir le Programmer (pince CH341A hors tension, ou on-device internal en SSH)
# → « Flasher cette unité avec le golden »  (backup pré-flash automatique)
```

Puis vérifier **sur l'unité** : `sudo odroid-check-deploy`.
