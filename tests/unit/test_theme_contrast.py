"""WCAG contrast of every text/background pair in the theme (AA = 4.5:1). Pure arithmetic, no Qt."""
import pytest

from hss.ui import theme


@pytest.mark.parametrize("name", sorted(theme.PAIRS))
def test_pair_meets_wcag_aa(name):
    fg, bg = theme.PAIRS[name]
    assert theme.contrast_ratio(fg, bg) >= 4.5, (name, fg, bg, theme.contrast_ratio(fg, bg))


def test_contrast_formula_known_values():
    assert round(theme.contrast_ratio("#000000", "#ffffff"), 2) == 21.0
    assert round(theme.contrast_ratio("#777777", "#ffffff"), 2) == 4.48          # classic AA boundary example


def test_old_unreadable_colors_would_fail():
    assert theme.contrast_ratio("#555555", theme.BG) < 4.5                        # the old muted text on dark
    assert theme.contrast_ratio("#c62828", theme.BG) < 4.5                        # the old red title on dark


def test_disabled_buttons_dimmer_but_readable():
    assert theme.BTN_DISABLED_TEXT != theme.BTN_TEXT
    assert theme.contrast_ratio(*theme.PAIRS["button-disabled"]) >= 4.5


def test_stylesheet_uses_only_declared_colors():
    import re
    used = set(re.findall(r"#[0-9a-fA-F]{6}", theme.APP_STYLESHEET))
    declared = {v.lower() for v in vars(theme).values() if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v)}
    assert {u.lower() for u in used} <= declared
