#!/usr/bin/env sh
# Désinstalleur du poste de déploiement Odroid-M1 — symétrique à install.sh.
#
# Défait ce qu'installe install.sh : service systemd kiosque, cible de boot,
# lanceur odroid-station. Par défaut ne touche PAS aux paquets apt (flashrom,
# xserver-xorg, … souvent utiles/partagés en dehors du kiosque) ni au checkout
# /opt/odroid_m1_deploy : passer --purge-packages et/ou --purge-repo pour
# aussi les supprimer (ou --all pour les deux).
#
#   sudo ./uninstall.sh                   # service + cible de boot + lanceur
#   sudo ./uninstall.sh --purge-repo      # + checkout /opt/odroid_m1_deploy
#   sudo ./uninstall.sh --purge-packages  # + paquets apt spécifiques au kiosque
#   sudo ./uninstall.sh --all             # tout
#
# Idempotent : peut être relancé sans erreur si déjà (partiellement) désinstallé.
set -eu

OPT_DIR="/opt/odroid_m1_deploy"
UNIT=/etc/systemd/system/odroid-station.service
BIN=/usr/local/bin/odroid-station

if [ "$(id -u)" = "0" ]; then
    SUDO=""
else
    SUDO="sudo"
fi

PURGE_PACKAGES=0
PURGE_REPO=0
for arg in "$@"; do
    case "$arg" in
        --purge-packages) PURGE_PACKAGES=1 ;;
        --purge-repo) PURGE_REPO=1 ;;
        --all) PURGE_PACKAGES=1; PURGE_REPO=1 ;;
        -h|--help)
            echo "Usage: $0 [--purge-packages] [--purge-repo] [--all]"
            exit 0
            ;;
        *)
            echo "Option inconnue: $arg (voir --help)" >&2
            exit 1
            ;;
    esac
done

# --- Service systemd : arrêt, désactivation, suppression de l'unité ---
if command -v systemctl >/dev/null 2>&1; then
    echo "Arrêt et désactivation du service odroid-station…"
    $SUDO systemctl stop odroid-station.service 2>/dev/null || true
    $SUDO systemctl disable odroid-station.service 2>/dev/null || true
    if [ -f "$UNIT" ]; then
        $SUDO rm -f "$UNIT"
        echo "  $UNIT supprimé"
    fi
    $SUDO systemctl daemon-reload
    # install.sh bascule sur graphical.target pour le boot kiosque ; on ne
    # restaure la cible standard que si c'est bien encore celle en place (pour
    # ne pas écraser un choix fait entre-temps par quelqu'un d'autre).
    if [ "$($SUDO systemctl get-default 2>/dev/null || true)" = "graphical.target" ]; then
        $SUDO systemctl set-default multi-user.target
        echo "  cible de boot restaurée sur multi-user.target"
    fi
else
    echo "⚠ systemd introuvable : rien à désactiver côté service."
fi

# --- Lanceur unique ---
if [ -e "$BIN" ]; then
    $SUDO rm -f "$BIN"
    echo "Lanceur supprimé : $BIN"
fi

# --- Paquets apt spécifiques au kiosque (optionnel) ---
if [ "$PURGE_PACKAGES" = "1" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        echo "Purge des paquets spécifiques au kiosque…"
        $SUDO apt-get purge -y \
            flashrom u-boot-tools mtd-utils python3-tk \
            xserver-xorg xinit x11-xserver-utils
        $SUDO apt-get autoremove -y
    else
        echo "⚠ apt introuvable : purge des paquets ignorée."
    fi
else
    echo "ℹ Paquets apt non touchés (util-linux/python3/parted/rsync/dosfstools/"
    echo "  e2fsprogs sont des paquets système souvent partagés) — relance avec"
    echo "  --purge-packages pour aussi retirer flashrom/u-boot-tools/mtd-utils/"
    echo "  python3-tk/xserver-xorg/xinit/x11-xserver-utils."
fi

# --- Checkout du repo (optionnel) ---
if [ "$PURGE_REPO" = "1" ]; then
    if [ -d "$OPT_DIR" ]; then
        echo "Suppression du checkout $OPT_DIR…"
        # Supprime aussi le répertoire du script lui-même si lancé depuis là :
        # sans risque, le shell garde le fichier ouvert (déjà lu) jusqu'à la fin.
        $SUDO rm -rf "$OPT_DIR"
    fi
else
    echo "ℹ Checkout $OPT_DIR non touché — relance avec --purge-repo pour le supprimer."
fi

echo
echo "Désinstallation terminée. Redémarre pour repasser en boot standard :"
echo "  sudo reboot"
