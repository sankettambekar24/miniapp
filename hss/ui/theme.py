"""Dark theme + dedicated banner styles. Every foreground/background pair here is checked by
tests/unit/test_theme_contrast.py (WCAG 2.x contrast ratio >= 4.5:1 for text)."""
from __future__ import annotations

BG = "#1b1d21"
PANEL = "#25282e"
TEXT = "#eceff4"
MUTED = "#b4bcc8"            # secondary text
BORDER = "#4a505a"
BTN_BG = "#323843"
BTN_TEXT = "#f2f4f8"
BTN_DISABLED_BG = "#2a2d33"
BTN_DISABLED_TEXT = "#a9b1bd"  # disabled = clearly dimmer, still readable
INPUT_BG = "#14161a"
SELECT_BG = "#3d6fb6"
SELECT_TEXT = "#ffffff"
LINK = "#8ab4f8"

# kind -> (background, foreground)
BANNERS = {
    "info": ("#0d47a1", "#ffffff"),
    "success": ("#1b5e20", "#ffffff"),
    "warning": ("#ffd54f", "#1b1b1b"),
    "error": ("#b71c1c", "#ffffff"),
}
ERROR_TEXT = "#ff9e9e"        # error text drawn directly on BG
WARN_TEXT = "#ffd54f"
OK_TEXT = "#8fdc9a"

# (foreground, background) pairs that must meet 4.5:1 (name -> pair) - used by the contrast test
PAIRS = {
    "body": (TEXT, BG), "panel": (TEXT, PANEL), "muted": (MUTED, BG), "muted-on-panel": (MUTED, PANEL),
    "button": (BTN_TEXT, BTN_BG), "button-disabled": (BTN_DISABLED_TEXT, BTN_DISABLED_BG),
    "input": (TEXT, INPUT_BG), "selection": (SELECT_TEXT, SELECT_BG), "link": (LINK, BG),
    "error-text": (ERROR_TEXT, BG), "warn-text": (WARN_TEXT, BG), "ok-text": (OK_TEXT, BG),
    **{f"banner-{k}": (fg, bg) for k, (bg, fg) in BANNERS.items()},
}


def _lin(c: int) -> float:
    v = c / 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def banner_style(kind: str) -> str:
    bg, fg = BANNERS[kind]
    return f"background:{bg};color:{fg};font-weight:bold;padding:6px;border-radius:3px;"


def muted_style() -> str:
    return f"color:{MUTED};"


APP_STYLESHEET = f"""
QWidget {{ background:{BG}; color:{TEXT}; }}
QLabel {{ background:transparent; }}
QMainWindow, QDialog {{ background:{BG}; }}
QPushButton {{ background:{BTN_BG}; color:{BTN_TEXT}; border:1px solid {BORDER}; padding:5px 12px; border-radius:3px; }}
QPushButton:hover {{ border-color:{LINK}; }}
QPushButton:disabled {{ background:{BTN_DISABLED_BG}; color:{BTN_DISABLED_TEXT}; border:1px solid {BTN_DISABLED_BG}; }}
QLineEdit, QPlainTextEdit, QComboBox {{ background:{INPUT_BG}; color:{TEXT}; border:1px solid {BORDER}; padding:3px; }}
QLineEdit:disabled, QComboBox:disabled {{ color:{BTN_DISABLED_TEXT}; }}
QTableWidget, QListWidget {{ background:{PANEL}; color:{TEXT}; gridline-color:{BORDER}; border:1px solid {BORDER}; }}
QTableWidget::item:selected, QListWidget::item:selected {{ background:{SELECT_BG}; color:{SELECT_TEXT}; }}
QListWidget::item:disabled {{ color:{BTN_DISABLED_TEXT}; }}
QHeaderView::section {{ background:{BTN_BG}; color:{BTN_TEXT}; border:1px solid {BORDER}; padding:4px; }}
QTabWidget::pane {{ border:1px solid {BORDER}; }}
QTabBar::tab {{ background:{BTN_BG}; color:{BTN_TEXT}; padding:6px 14px; border:1px solid {BORDER}; }}
QTabBar::tab:selected {{ background:{SELECT_BG}; color:{SELECT_TEXT}; }}
QStatusBar {{ background:{PANEL}; color:{TEXT}; }}
QToolTip {{ background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; }}
QProgressBar {{ border:1px solid {BORDER}; color:{TEXT}; text-align:center; }}
QProgressBar::chunk {{ background:{SELECT_BG}; }}
QCheckBox {{ color:{TEXT}; }}
"""


def apply(app) -> None:
    """Fusion style + the dark stylesheet (keeps the dark look independent of the Windows theme)."""
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
