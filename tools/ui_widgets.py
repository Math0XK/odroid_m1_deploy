#!/usr/bin/env python3
"""
ui_widgets.py — briques d'interface PARTAGÉES par les onglets du tableau de
bord (`station.py`) : bandeau d'état, journal en couleurs, styles communs.

Objectif de la refonte : qu'on sache D'UN COUP D'ŒIL où en est l'opération
(bandeau), ce qui s'est bien passé (vert), ce qui mérite attention (orange),
ce qui a échoué (rouge) — au lieu du mur de texte uniforme d'avant. Le
`LogView` consomme directement les niveaux du `report.Reporter` : les moteurs
n'ont RIEN à savoir de tkinter.

Tout est du tkinter/ttk standard (paquet python3-tk du poste kiosque), pensé
tactile : grandes polices, gros boutons, contrastes nets.
"""

import tkinter as tk
from tkinter import ttk

# États du bandeau -> (fond, texte)
_BANNER_COLORS = {
    "idle":  ("#e8eaed", "#333333"),
    "busy":  ("#1a4b8c", "#ffffff"),
    "ok":    ("#1e7d32", "#ffffff"),
    "warn":  ("#b45f06", "#ffffff"),
    "error": ("#b3261e", "#ffffff"),
}

# Niveaux du Reporter -> style de ligne dans le journal
_LOG_TAGS = {
    "step":   {"foreground": "#1a4b8c", "font": ("TkDefaultFont", 12, "bold"),
               "spacing1": 10, "spacing3": 2},
    "info":   {"foreground": "#222222"},
    "ok":     {"foreground": "#1e7d32"},
    "warn":   {"foreground": "#b45f06"},
    "error":  {"foreground": "#b3261e", "font": ("TkDefaultFont", 11, "bold")},
    "detail": {"foreground": "#777777", "lmargin1": 28, "lmargin2": 28},
    "cmd":    {"foreground": "#555555", "font": ("TkFixedFont", 10),
               "lmargin1": 20, "lmargin2": 20},
}

_LOG_PREFIX = {
    "info": "", "ok": "✔ ", "warn": "⚠ ", "error": "✖ ",
    "detail": "", "cmd": "$ ", "step": "",
}


class StatusBanner(tk.Label):
    """Bandeau d'état plein-largeur : UNE phrase, colorée selon l'état.
    `set_state("busy", "Étape 3/10 — Copie des données…")`."""

    def __init__(self, master):
        super().__init__(master, text="Prêt.", anchor="w",
                         font=("TkDefaultFont", 13, "bold"), padx=12, pady=8)
        self.set_state("idle", "Prêt.")

    def set_state(self, state, text):
        bg, fg = _BANNER_COLORS.get(state, _BANNER_COLORS["idle"])
        self.config(text=text, background=bg, foreground=fg)


class LogView(ttk.Frame):
    """Journal en couleurs, alimenté par les niveaux du `report.Reporter`
    (`write(level, text)`). Lecture seule, auto-scroll, étapes en gras."""

    def __init__(self, master, height=14):
        super().__init__(master)
        self.text = tk.Text(self, height=height, state="disabled", wrap="word",
                            font=("TkDefaultFont", 11), background="#fcfcfc",
                            padx=8, pady=6, borderwidth=1, relief="solid")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, cfg in _LOG_TAGS.items():
            self.text.tag_configure(tag, **cfg)

    def write(self, level, message):
        prefix = _LOG_PREFIX.get(level, "")
        self.text.configure(state="normal")
        self.text.insert(tk.END, f"{prefix}{message}\n", (level,))
        self.text.see(tk.END)
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.configure(state="disabled")


class ScrollableFrame(ttk.Frame):
    """Zone défilante verticale, hauteur FIXE : les contrôles de config d'un
    onglet peuvent dépasser la hauteur d'écran (variable selon le poste, cf.
    docs/DEPLOIEMENT_FLOTTE.md §2) sans jamais pousser le bandeau d'état ou le
    journal hors champ. Monter les widgets du panel dans `.body`, PAS `self`."""

    def __init__(self, master, height):
        super().__init__(master)
        canvas = tk.Canvas(self, highlightthickness=0, height=height)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(window_id, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Molette (utile en dev/SSH+X ; le poste kiosque est tactile, la
        # scrollbar visible reste l'affordance principale) — active seulement
        # au survol pour ne pas capter le défilement d'un autre onglet.
        def _scroll(event):
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
        canvas.bind("<Enter>", lambda _e: (
            canvas.bind_all("<Button-4>", _scroll),
            canvas.bind_all("<Button-5>", _scroll)))
        canvas.bind("<Leave>", lambda _e: (
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>")))


def scroll_height(widget):
    """Hauteur (px) allouée à la zone de config défilante d'un panel : une
    fraction de l'écran RÉEL (pas une constante — les écrans varient selon le
    poste), qui laisse toujours une part généreuse à la barre d'état/le
    journal, packés après (voir chaque panel `_build_ui`)."""
    return max(140, int(widget.winfo_screenheight() * 0.36))


def section(parent, title):
    """LabelFrame de section numérotée, style commun aux onglets
    (« 1 · Source », « 2 · Destination »…)."""
    frame = ttk.LabelFrame(parent, text=f" {title} ")
    frame.pack(fill="x", padx=10, pady=6)
    return frame


def hint(parent, text):
    """Ligne d'aide grisée sous un contrôle (texte court, wrap large)."""
    lbl = ttk.Label(parent, text=text, foreground="#666", wraplength=900,
                    justify="left")
    lbl.pack(anchor="w", padx=8, pady=(2, 6))
    return lbl
