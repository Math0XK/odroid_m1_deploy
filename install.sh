#!/usr/bin/env sh
# Installeur du poste de déploiement Odroid-M1 (Linux, image fraîche/minimale).
#
# Installation EN UNE LIGNE depuis une image vierge (rien d'autre à préparer) :
#
#   curl -fsSL https://raw.githubusercontent.com/Math0XK/odroid_m1_deploy/main/install.sh | sudo sh
#   sudo reboot          # ou : sudo systemctl start odroid-station
#
# Lancé hors checkout (curl | sh), ce script se BOOTSTRAPPE : il installe git,
# clone le repo dans /opt/odroid_m1_deploy puis relance le install.sh du
# checkout. Depuis un checkout existant, l'usage classique reste :
#
#   git clone https://github.com/Math0XK/odroid_m1_deploy.git
#   cd odroid_m1_deploy
#   sudo ./install.sh
#
# Installe TOUTES les dépendances (flashrom, u-boot-tools, clonage, X minimal),
# crée des lanceurs dans /usr/local/bin, et installe un service systemd
# (`odroid-station.service`) qui lance le tableau de bord unifié en mode kiosque
# plein écran à chaque démarrage — le poste n'a besoin d'AUCUN geste manuel après
# le premier boot. Conçu pour une image Ubuntu/Armbian minimale SANS
# gestionnaire de bureau (comme l'image ODROID-M1 vendor) : ce script démarre X
# lui-même, il n'installe ni GDM ni LightDM ni de window manager.
#
# Idempotent : peut être relancé après un `git pull` (écrase le service, ne
# duplique rien). Relancer la ligne curl met aussi à jour un /opt existant
# (`git pull` avant réinstallation).
set -eu

REPO_URL="https://github.com/Math0XK/odroid_m1_deploy.git"
OPT_DIR="/opt/odroid_m1_deploy"

# Une image minimale n'a pas toujours `sudo` ; en root il est superflu.
if [ "$(id -u)" = "0" ]; then
    SUDO=""
else
    SUDO="sudo"
fi

# Sur une image fraîche, unattended-upgrades tient souvent le verrou apt/dpkg au
# premier boot : sans ça, apt échoue sur « Could not get lock ». DPkg::Lock::Timeout
# fait patienter apt (jusqu'à 10 min) qu'il se libère au lieu d'abandonner.
APT="apt-get -o DPkg::Lock::Timeout=600"

# --- Bootstrap (curl | sh) : pas de checkout à côté de ce script -> on clone ---
# Détection : lancé depuis un pipe, $0 vaut "sh" (ou similaire) et il n'y a pas
# de tools/station.py à côté. Depuis un checkout, le fichier existe.
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo /nonexistent)"
if [ ! -f "$SCRIPT_DIR/tools/station.py" ]; then
    echo "Aucun checkout local détecté : bootstrap depuis $REPO_URL…"
    if ! command -v git >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
            $SUDO $APT update
            $SUDO $APT install -y git ca-certificates
        else
            echo "⚠ git introuvable et pas d'apt : installe git puis relance." >&2
            exit 1
        fi
    fi
    if [ -d "$OPT_DIR/.git" ]; then
        echo "Checkout existant dans $OPT_DIR : mise à jour (git pull)…"
        $SUDO git -C "$OPT_DIR" pull --ff-only
    else
        $SUDO git clone "$REPO_URL" "$OPT_DIR"
    fi
    exec sh "$OPT_DIR/install.sh"
fi

PKG="$SCRIPT_DIR"

# --- Dépendances ---
if command -v apt-get >/dev/null 2>&1; then
    echo "Installation des dépendances (apt)…"
    $SUDO $APT update
    $SUDO $APT install -y \
        flashrom u-boot-tools mtd-utils \
        python3 python3-tk \
        util-linux parted rsync dosfstools e2fsprogs partclone \
        xserver-xorg xinit x11-xserver-utils
else
    echo "⚠ apt introuvable : installe manuellement flashrom, u-boot-tools,"
    echo "  mtd-utils, python3-tk, parted, rsync, dosfstools, e2fsprogs,"
    echo "  partclone, xserver-xorg, xinit, x11-xserver-utils."
fi

# --- Lanceur UNIQUE : odroid-station (GUI sans argument, CLI en sous-commandes) ---
BIN=/usr/local/bin
echo "Création du lanceur dans $BIN…"
tmp="$(mktemp)"
printf '#!/usr/bin/env sh\nexec python3 "%s/tools/station.py" "$@"\n' "$PKG" > "$tmp"
$SUDO install -m 0755 "$tmp" "$BIN/odroid-station"
rm -f "$tmp"
echo "  $BIN/odroid-station -> tools/station.py"
# Ménage : lanceurs des anciennes versions (outils désormais fusionnés).
for old in odroid-spi-flash odroid-clone odroid-clone-cli odroid-check-deploy; do
    if [ -e "$BIN/$old" ]; then
        $SUDO rm -f "$BIN/$old"
        echo "  (ancien lanceur $old supprimé)"
    fi
done

# --- Vérif du golden si présent ---
GOLDEN="$PKG/images/spi/golden_spi_16MiB.bin"
if [ -f "$GOLDEN" ] && [ -f "$GOLDEN.sha256" ]; then
    echo "Vérification du golden SPI…"
    ( cd "$PKG/images/spi" && sha256sum -c "$(basename "$GOLDEN").sha256" )
else
    echo "ℹ Pas encore de golden SPI ($GOLDEN) — le produire avec 'odroid-station'"
    echo "  (onglet SPI → « Lire la puce → golden », ou 'odroid-station spi read')"
    echo "  puis le committer."
fi

# --- Service systemd : boot kiosque (X + tableau de bord plein écran) ---
if command -v systemctl >/dev/null 2>&1; then
    echo "Installation du service systemd odroid-station…"
    UNIT=/etc/systemd/system/odroid-station.service
    sed "s#@REPO_ROOT@#$PKG#g" "$PKG/station/odroid-station.service" \
        | $SUDO tee "$UNIT" >/dev/null
    $SUDO chmod +x "$PKG/station/xinitrc"
    $SUDO systemctl daemon-reload
    # graphical.target existe toujours (target de base systemd) même sans
    # gestionnaire de bureau installé — pas besoin de GDM/LightDM pour l'atteindre.
    $SUDO systemctl set-default graphical.target
    $SUDO systemctl enable odroid-station.service
    KIOSK_INSTALLED=1
else
    echo "⚠ systemd introuvable : le boot kiosque automatique n'est pas installé."
    echo "  Lance le tableau de bord manuellement : sudo odroid-station"
    KIOSK_INSTALLED=0
fi

cat <<EOF

Installé. UN SEUL outil : odroid-station (root requis pour flasher / cloner).
  sudo odroid-station                  # tableau de bord graphique (3 onglets)
En SSH sans X11, les mêmes opérations en sous-commandes :
  sudo odroid-station list             # disques détectés
  sudo odroid-station clone …          # clonage disque/image -> disque
  sudo odroid-station image …          # image disque COMPACTE (source de clonage)
  sudo odroid-station spi …            # read / verify / flash / env-apply / env-save
  sudo odroid-station check            # vérif post-déploiement GO/NO-GO
  odroid-station --help                # détail des options (--sim, --yes, …)

Runbook : $PKG/docs/DEPLOIEMENT_FLOTTE.md
EOF

if [ "$KIOSK_INSTALLED" = "1" ]; then
    cat <<EOF
Boot kiosque installé (service odroid-station, cible: graphical.target).
Le tableau de bord se lancera automatiquement plein écran à chaque démarrage.

  sudo reboot                              # démarrer en mode kiosque maintenant
  sudo systemctl start odroid-station      # ou : lancer sans redémarrer
  journalctl -u odroid-station -f          # débogage du service
  sudo systemctl disable odroid-station    # désactiver le boot kiosque

⚠ Ne lance PAS 'systemctl start' depuis une session déjà connectée sur tty1 : le
  service prend le contrôle de ce terminal (chvt 1). Préfère 'reboot', ou lance
  depuis une session SSH / un autre tty.
EOF
fi
