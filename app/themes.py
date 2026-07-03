"""
themes — application colour schemes applied via QPalette (Fusion style), so every
widget (tree, editor, hex view, menus) is themed consistently.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

# name -> color spec. None = the default light scheme.
THEMES = {
    "Light": None,
    "Dark": {
        "window": "#2b2b2e", "text": "#e0e0e0", "base": "#1e1e1e",
        "alt": "#2b2b2e", "button": "#3a3a3e", "highlight": "#2f5f8f",
        "hltext": "#ffffff", "tooltip_bg": "#3a3a3e", "link": "#5aa0e0",
        "placeholder": "#8a8a8a", "disabled": "#7a7a7a",
    },
    "Midnight Blue": {
        "window": "#1f2733", "text": "#d6e0ef", "base": "#161c25",
        "alt": "#1f2733", "button": "#2a3543", "highlight": "#3d6fb5",
        "hltext": "#ffffff", "tooltip_bg": "#2a3543", "link": "#7fb0ee",
        "placeholder": "#7e8aa0", "disabled": "#73809a",
    },
    "Solarized Dark": {
        "window": "#002b36", "text": "#93a1a1", "base": "#073642",
        "alt": "#002b36", "button": "#0a4955", "highlight": "#268bd2",
        "hltext": "#fdf6e3", "tooltip_bg": "#0a4955", "link": "#2aa198",
        "placeholder": "#657b83", "disabled": "#586e75",
    },
}


def theme_names():
    return list(THEMES)


def is_dark(name: str) -> bool:
    return THEMES.get(name) is not None


def apply_theme(app, name: str):
    spec = THEMES.get(name)
    app.setStyle("Fusion")
    if spec is None:                      # Light / default
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")
        return
    c = {k: QColor(v) for k, v in spec.items()}
    p = QPalette()
    p.setColor(QPalette.Window, c["window"])
    p.setColor(QPalette.WindowText, c["text"])
    p.setColor(QPalette.Base, c["base"])
    p.setColor(QPalette.AlternateBase, c["alt"])
    p.setColor(QPalette.ToolTipBase, c["tooltip_bg"])
    p.setColor(QPalette.ToolTipText, c["text"])
    p.setColor(QPalette.Text, c["text"])
    p.setColor(QPalette.Button, c["button"])
    p.setColor(QPalette.ButtonText, c["text"])
    p.setColor(QPalette.BrightText, QColor("#ff5555"))
    p.setColor(QPalette.Link, c["link"])
    p.setColor(QPalette.Highlight, c["highlight"])
    p.setColor(QPalette.HighlightedText, c["hltext"])
    p.setColor(QPalette.PlaceholderText, c["placeholder"])
    for grp in (QPalette.Disabled,):
        p.setColor(grp, QPalette.Text, c["disabled"])
        p.setColor(grp, QPalette.WindowText, c["disabled"])
        p.setColor(grp, QPalette.ButtonText, c["disabled"])
    app.setPalette(p)
    # Tooltips + group-box titles read better with an explicit border on dark.
    app.setStyleSheet(
        "QToolTip { color: %s; background: %s; border: 1px solid %s; }"
        % (spec["text"], spec["tooltip_bg"], spec["button"]))
