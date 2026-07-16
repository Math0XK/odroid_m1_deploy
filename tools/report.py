#!/usr/bin/env python3
"""
report.py — journal structuré partagé par TOUTES les interfaces (GUI + CLI).

Un `Reporter` remplace le simple callback `log(texte)` d'avant : chaque ligne
porte un NIVEAU (étape numérotée, info, ok, attention, erreur, commande,
détail), si bien que chaque interface peut la PRÉSENTER clairement — couleurs
et gras dans la GUI (`ui_widgets.LogView`), préfixes lisibles dans le terminal
(`console_sink`) — au lieu du mur de texte uniforme.

Le Reporter COLLECTE aussi les avertissements et erreurs au fil de l'eau : en
fin d'opération les moteurs affichent un RÉCAPITULATIF (`summary_lines`) qui
rappelle ce qui mérite attention, sans obliger à relire tout le journal.

API côté moteur (clone_engine, spi_ops…) :
    r.begin(9)                       # nombre d'étapes de l'opération
    r.step("Formatage des partitions")   # -> « ÉTAPE 3/9 — Formatage… »
    r.info("...")  r.ok("...")  r.warn("...")  r.error("...")
    r.detail("...")                  # ligne secondaire (sortie de commande…)
    r.cmd("sfdisk --wipe ...")       # commande exécutée
    r.progress(42.0, "Copie… 42 %")  # relaie vers l'interface (barre)

Côté interface, `sink(level, text)` reçoit chaque ligne avec son niveau parmi
`LEVELS`. `text` peut être multi-ligne : c'est au sink de l'indenter.
"""

LEVELS = ("step", "info", "ok", "warn", "error", "detail", "cmd")


class Reporter:
    """Un Reporter = une opération journalisée. `sink(level, text)` reçoit
    chaque ligne ; `progress(pct, text)` (optionnel) alimente une barre de
    progression. Thread-safe tant que le sink l'est (queue GUI, print)."""

    def __init__(self, sink, progress=None):
        self._sink = sink
        self._progress = progress if progress is not None else (lambda p, t: None)
        self.total_steps = 0
        self.step_no = 0
        self.warnings = []
        self.errors = []

    # ---------- Cycle de vie ----------
    def begin(self, total_steps):
        """Démarre une opération de `total_steps` étapes numérotées."""
        self.total_steps = total_steps
        self.step_no = 0
        self.warnings = []
        self.errors = []

    def step(self, title):
        """Étape numérotée suivante — la charpente du journal."""
        self.step_no += 1
        if self.total_steps:
            self._sink("step", f"ÉTAPE {self.step_no}/{self.total_steps} — {title}")
        else:
            self._sink("step", title)

    # ---------- Lignes ----------
    def info(self, text):
        self._sink("info", text)

    def ok(self, text):
        self._sink("ok", text)

    def warn(self, text):
        self.warnings.append(text)
        self._sink("warn", text)

    def error(self, text):
        self.errors.append(text)
        self._sink("error", text)

    def detail(self, text):
        self._sink("detail", text)

    def cmd(self, text):
        self._sink("cmd", text)

    # ---------- Progression ----------
    def progress(self, pct, text):
        self._progress(pct, text)

    # ---------- Bilan ----------
    def summary_lines(self):
        """Bilan compact de l'opération (à afficher dans le récapitulatif) :
        rappelle chaque avertissement/erreur collecté, ou « rien à signaler »."""
        lines = []
        if not self.warnings and not self.errors:
            lines.append("Aucun avertissement : rien à signaler.")
            return lines
        if self.errors:
            lines.append(f"{len(self.errors)} erreur(s) :")
            lines.extend(f"  ✖ {e.splitlines()[0]}" for e in self.errors)
        if self.warnings:
            lines.append(f"{len(self.warnings)} avertissement(s) :")
            lines.extend(f"  ⚠ {w.splitlines()[0]}" for w in self.warnings)
        return lines


# ---------------------------------------------------------------------------
# Sinks prêts à l'emploi
# ---------------------------------------------------------------------------
_ANSI = {
    "step": "\033[1;36m",     # gras cyan
    "ok": "\033[32m",         # vert
    "warn": "\033[33m",       # jaune
    "error": "\033[1;31m",    # gras rouge
    "detail": "\033[2m",      # atténué
    "cmd": "\033[2m",
    "reset": "\033[0m",
}

_PREFIX = {
    "step": "",
    "info": "  ",
    "ok": "  ✔ ",
    "warn": "  ⚠ ",
    "error": "  ✖ ",
    "detail": "      ",
    "cmd": "  $ ",
}


def console_sink(color=None):
    """Sink terminal : préfixes lisibles + couleurs ANSI si tty.

    `color=None` auto-détecte (isatty) ; True/False force. Les textes
    multi-lignes sont indentés sous leur préfixe. Robuste aux consoles
    non-UTF-8 (les glyphes non encodables sont remplacés, jamais de crash).
    """
    import sys
    if color is None:
        color = bool(getattr(sys.stdout, "isatty", lambda: False)())

    def sink(level, text):
        prefix = _PREFIX.get(level, "  ")
        pad = " " * len(prefix)
        lines = str(text).splitlines() or [""]
        body = ("\n" + pad).join(lines)
        out = f"{prefix}{body}"
        if level == "step":
            out = f"\n━━ {body} " + "━" * max(4, 58 - len(body))
        if color and level in _ANSI:
            out = f"{_ANSI[level]}{out}{_ANSI['reset']}"
        try:
            print(out, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(out.encode(enc, "replace").decode(enc), flush=True)

    return sink


def plain_sink(write):
    """Adapte un callback texte pur `write(str)` (tests, compat) en sink."""
    def sink(level, text):
        prefix = _PREFIX.get(level, "  ")
        write(f"{prefix}{text}")
    return sink
