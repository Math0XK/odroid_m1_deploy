# Odroid M1 — Boot NVMe direct + NPU fonctionnel : note de passation

## Contexte

Projet Harvest (détection de rats par vision embarquée). Cible de production : Odroid M1
(RK3568B2, 8GB RAM). Objectif de cette session : faire booter l'Ubuntu 22.04.5
(kernel vendor `5.10.0-odroid-arm64`, avec driver NPU RKNPU) **directement depuis un SSD
NVMe** (Fanxiang S501 128GB), **sans passer par le kexec de Petitboot** (l'image Ubuntu
avait des incompatibilités avec ce flow), tout en gardant le NPU fonctionnel.

**Résultat : objectif atteint et validé** — boot 100% automatique au power-on, NPU
confirmé opérationnel par inférence réelle (démo `rknn_yolov5_demo`, ~63ms/run,
détections cohérentes).

Ce qui reste à faire, et qui est délégué à Claude Code : **construire un système
reproductible de backup/clonage** de cette configuration (SSD NVMe + bootloader SPI),
car à l'heure actuelle tout repose sur un unique exemplaire physique de chaque
composant, sans sauvegarde robuste hors-device.

---

## Architecture de boot finale

```
Power on
  → Boot ROM RK3568 (cherche SPI en premier — confirmé empiriquement, pas SD/eMMC)
  → SPI flash (16 MiB, 5 partitions MTD) :
      mtd0 "SPL"         ← idbloader-spi.img (Armbian)
      mtd1 "U-Boot Env"  ← environnement U-Boot (persiste les vars via fw_setenv)
      mtd2 "U-Boot"      ← u-boot-rockchip-spi.bin (Armbian, U-Boot 2026.01, bootstd)
      mtd3 "splash"
      mtd4 "Filesystem"
  → U-Boot 2026.01_armbian exécute bootcmd = "run distro_bootcmd"
      (PAS le bootcmd par défaut "bootflow scan -lb" — l'env actif est resté sur
       l'ancien mécanisme distroboot legacy, jamais resynchronisé avec les defaults
       compilés dans le binaire v2026.01 ; voir "Piège #1" plus bas)
  → distro_bootcmd itère boot_targets = "nvme mmc1 mmc0 mtd2 mtd1 mtd0 usb0 pxe dhcp"
  → cible "nvme" en premier :
      run nvme_boot → active le régulateur NPU, scanne le bus PCIe/NVMe,
      trouve /boot.scr sur nvme0n1p1 (partition BOOT, ext2), l'exécute
  → boot.scr (flash-kernel, modifié) charge vmlinuz/initrd/dtb depuis nvme0n1p1,
      passe les bootargs (incl. cma=128M), boot Ubuntu 22.04.5 sur nvme0n1p2 (rootfs, ext4)
  → si "nvme" échoue à n'importe quelle étape → fallback automatique et propre sur
      "mmc1" (Armbian sur carte SD) — filet de sécurité toujours actif
```

---

## Root causes identifiées et fixes appliqués

### 1. NVMe non détecté par le mécanisme de scan automatique
**Cause :** le `nvme_boot` par défaut fait juste `nvme scan` sans `pci enum` préalable.
Le lien PCIe (RK3568 ↔ contrôleur NVMe Realtek 0x10ec:0x5765) a besoin d'un
`pci enum` explicite pour terminer son link training avant que `nvme scan` puisse
trouver le device. Sans ça : `Device 0: unknown device`.
**Fix :** `pci enum;` ajouté en tête de la macro `nvme_boot`.

### 2. Kernel panic au boot : NPU power domain
```
rockchip-pm-domain fdd90000.power-management:power-controller: failed to get ack on domain 'npu'
Kernel panic - not syncing: panic_on_set_idle set ...
```
**Cause (confirmée par un mainteneur Armbian sur `armbian/linux-rockchip#297`,
bug générique RK3568, pas spécifique à ce board) :** le domaine d'alimentation NPU
ne reçoit son ACK que si son régulateur de tension est activé au préalable. Sur
RK3568/RK3566, ce n'est pas automatique — chaque board a besoin d'un réglage
explicite. Le firmware Hardkernel d'origine (Petitboot) le faisait ; le U-Boot
Armbian flashé ne le fait pas.
**Régulateur concerné (confirmé via device tree officiel `rk3568-odroid-m1.dts`) :**
`vdd_npu` = `DCDC_REG4` sur le PMIC RK809 (I2C, `pmic@20`). Plage : 500mV–1350mV,
valeur nominale du DT : 900mV.
**Fix (testé en live via console U-Boot, puis rendu permanent) :**
```
regulator dev vdd_npu
regulator value 900000
regulator enable
```

### 3. RKNPU driver charge mais échoue à allouer ses buffers
```
cma: cma_alloc: reserved: alloc failed, req-size: 1560 pages, ret: -12
RKNPU fde40000.npu: RKNPU: failed to allocate 6389760 buffer.
```
**Cause :** `RKNPU fde40000.npu: rknpu iommu is disabled, using non-iommu mode` — sur
ce kernel vendor, sans IOMMU, le NPU dépend d'une zone CMA réservée. Le DTB de base
ne définit pas de node `reserved-memory` nommé dédié au NPU → le kernel retombe sur
la valeur par défaut `CONFIG_CMA_SIZE_MBYTES` = 16 MiB, insuffisant.
**Fix :** ajout de `cma=128M` aux bootargs du kernel, via `/boot.scr` sur la
partition BOOT du NVMe (généré par `flash-kernel`, format image U-Boot compilée).

### Piège technique à connaître : corruption silencieuse via round-trip dumpimage/mkimage
`dumpimage -T script -p 0 -o fichier.cmd boot.scr` puis `sed` sur le fichier extrait
puis `mkimage -C none -A arm64 -T script -d fichier.cmd boot.scr` a produit un script
qui échouait immédiatement (`SCRIPT FAILED: continuing...`) au chargement par U-Boot,
sans erreur visible dans le fichier texte extrait (contenu vérifié correct via `grep`/`cat`).
Cause exacte non identifiée avec certitude (suspicion : gestion des fins de ligne ou
d'un octet de tête lors du round-trip binaire). **Solution fiable : ne jamais repartir
d'un fichier extrait via `dumpimage`. Toujours écrire le `.cmd` source à la main
(heredoc / fichier neuf) à partir du contenu texte connu, puis compiler direct avec
`mkimage`.** C'est cette méthode qui a fonctionné de façon fiable.

---

## État actuel du système (à la fin de cette session)

### Variables d'environnement U-Boot actives (persistées via `fw_setenv`, stockées dans mtd1 "U-Boot Env")
```
boot_targets=nvme mmc1 mmc0 mtd2 mtd1 mtd0 usb0 pxe dhcp
npu_regulator_enable=regulator dev vdd_npu; regulator value 900000; regulator enable
nvme_boot=run npu_regulator_enable; pci enum; nvme scan; if nvme device ${devnum}; then setenv devtype nvme; run scan_dev_for_boot_part; fi
bootcmd_nvme=setenv devnum 0; run nvme_boot
```
Ces variables ont été ajoutées **en plus** de l'environnement distroboot legacy déjà
présent (jamais resynchronisé avec les defaults du binaire U-Boot 2026.01 — le
mécanisme de reset auto au premier boot, contrôlé par la variable `armbian`, avait
déjà été consommé par une install antérieure). `sudo fw_printenv` pour voir l'état
complet actuel.

### Contenu de `/boot.scr` sur `nvme0n1p1` (partition BOOT, ext2)
Script `flash-kernel` (`bootscr.odroid-rk3568`) modifié pour inclure `cma=128M`
dans les bootargs. Version de référence stockée dans cette même partition sous
`/boot.scr.backup_20260710` (copie d'avant modification, SANS le fix cma).
Le fichier `/boot.scr.bak` présent sur le disque est encore plus ancien (7-8 juillet,
antérieur à cette session — probablement lié au fix UUID hardcodé d'une session précédente).

### Backups réalisés pendant cette session (⚠️ emplacements fragiles, voir tâche ci-dessous)
- Dump complet de la SPI d'origine (avant tout flash), 5 partitions séparées :
  `~/spi_backup_mtd{0,1,2,3,4}_20260710.bin` (sur la carte SD Armbian, home de `odroid`)
- Dump de l'environnement U-Boot d'origine (avant nos modifs) :
  `~/uboot_env_backup_YYYYMMDD_HHMM.txt` (idem, sur SD Armbian)
- `/boot/boot.scr.backup_20260710` sur le NVMe (copie pré-modif du boot script)

### Partitions NVMe (Fanxiang S501 128GB)
```
nvme0n1p1  BOOT    ext2   UUID=ec412577-5be1-4827-85c0-62d134dfa9b7
nvme0n1p2  rootfs  ext4   UUID=a9bdb4f9-07b2-4cae-a120-2c08783df4fd
```
Root FS UUID référencé dans `/boot.scr` (`root=UUID=eee2b90d-...` — **note : cette UUID
diffère de celle de `nvme0n1p2` lsblk `a9bdb4f9-...`**, à vérifier/clarifier — possible
qu'il s'agisse de l'UUID du filesystem interne vs celui de la partition, ou résidu
d'un ancien fix ; à confirmer avant toute opération de clonage pour éviter de
reproduire une image avec un mauvais root UUID).

---

## Tâche pour Claude Code

Construire un système reproductible pour :

1. **Cloner proprement le SSD NVMe** (les deux partitions, BOOT + rootfs) vers une
   image de sauvegarde et/ou un second SSD physique, en évitant les problèmes déjà
   rencontrés par le passé sur ce projet (collisions UUID/PARTUUID entre source et
   clone — voir historique Harvest / NVMe boot Petitboot). Il faut que le clone soit
   soit identique bit-à-bit (dd complet), soit régénéré avec des UUID cohérents et
   le `/boot.scr` correctement réécrit si les UUID changent.

2. **Sauvegarder la configuration SPI/U-Boot de façon durable et hors-device** —
   actuellement les dumps SPI et l'env backup ne vivent que sur la carte SD Armbian
   (single point of failure). Il faut les rapatrier vers un stockage durable
   (dépôt git, NAS, etc.) et documenter/scripter la restauration complète :
   - reflash des 5 partitions MTD depuis les `.bin` de backup (`flashcp` ou `dd`
     vers `/dev/mtdblockN`)
   - réapplication des 4 variables d'env critiques listées ci-dessus via `fw_setenv`
     (à faire APRÈS le reflash SPI, puisque `fw_setenv` écrit dans mtd1 qui aura
     été remis à l'état d'origine par le restore)

3. **Documenter/scripter un déploiement complet sur un Odroid M1 + NVMe neufs** —
   à partir de : l'image Hardkernel Ubuntu 22.04.5 d'origine + `armbian-install`
   option 7 (flash SPI, PAS l'option 4 qui wipe/installe un rootfs Armbian) + les
   4 `fw_setenv` + la modification `cma=128M` dans le `boot.scr` (méthode heredoc,
   PAS dumpimage/sed/mkimage — voir piège documenté plus haut).

4. **Écrire un script de vérification post-déploiement** qui confirme automatiquement
   que le nouveau système est sain : boot sur NVMe réussi (`uname -r` =
   `5.10.0-odroid-arm64`), absence des erreurs connues (`failed to get ack`,
   `failed to allocate`), présence de `/dev/dri/card0`+`renderD128`, et idéalement
   un test d'inférence NPU minimal (ex: relancer `rknn_yolov5_demo` et vérifier
   qu'il produit des détections avec un temps de run cohérent, <100ms).

**Point d'attention pour Claude Code :** avant de committer à une stratégie de
clonage, clarifier la divergence d'UUID rootfs mentionnée plus haut
(`eee2b90d-...` dans boot.scr vs `a9bdb4f9-...` vu par `lsblk`) — cette
incohérence doit être comprise pour ne pas propager un système qui boot sur un
mauvais device par coïncidence de nommage.
