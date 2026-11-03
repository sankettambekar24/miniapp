"""UI smoke tests on the offscreen Qt platform. Status: MOCK-TESTED."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from hss.config import MODE_PRODUCTION
from hss.ui.main_window import BANNER_TEXT, PAGE_ADMIN, PAGE_AUTH, PAGE_USER, MainWindow

from conftest import ST, Rig


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_test_mode_banner_and_title(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    assert not w.banner.isHidden() and w.banner.text() == BANNER_TEXT
    assert w.mode_title.text() == "PIN-ONLY TEST MODE"
    assert "PIN-ONLY TEST MODE" in w.mode_label.text()


def test_production_mode_ui_is_distinct_and_password_based(app):
    rig = Rig([ST], mode=MODE_PRODUCTION)
    w = MainWindow(rig.ctl)
    assert w.banner.isHidden() and not w.mode_banner.isHidden()           # blue production banner, no red test banner
    assert "PRODUCTION" in w.mode_banner.text()
    assert w.mode_title.text() == "Password login (no hardware factor)"
    assert not w.login_box.isHidden() and w.start_btn.isHidden() and w.factor_box.isHidden()
    assert not w.signout_btn.isEnabled() and w.signout_btn.toolTip()
    assert "[PIN TEST MODE]" not in w.windowTitle()


def test_test_mode_ui_stays_red_and_has_no_password_form(app):
    w = MainWindow(Rig([ST]).ctl)
    assert not w.banner.isHidden() and w.mode_banner.isHidden() and w.login_box.isHidden()
    assert "[PIN TEST MODE]" in w.windowTitle()


def test_factors_are_disabled_stubs(app):
    w = MainWindow(Rig([ST]).ctl)
    texts = [c.text() for c in w.findChildren(type(w.mode_title)) if "disabled stub" in c.text()]
    assert len(texts) == 2 and all("Not available" in t for t in texts)


def test_role_routing_and_lock_on_unplug(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    rig.connect()
    w.refresh()
    assert w.start_btn.isEnabled()
    rig.ctl.begin_auth()
    rig.device_says("AUTH_PIN_VERIFIED:2")
    w.refresh()
    assert w.stack.currentIndex() == PAGE_USER  # user slot -> user dashboard, no selector
    assert not (w.sidebar.item(PAGE_ADMIN).flags() & Qt.ItemIsEnabled)
    rig.unplug()
    w.refresh()
    assert w.stack.currentIndex() == PAGE_AUTH
    assert "locked" in w.state_label.text().lower()


def test_admin_slot_routes_to_admin(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says("AUTH_PIN_VERIFIED:1")
    w.refresh()
    assert w.stack.currentIndex() == PAGE_ADMIN


def test_diagnostics_page_lists_ports_and_rescan(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    rig.connect()
    w.diag_page.refresh()
    assert w.diag_page.table.rowCount() == 1 and w.diag_page.table.item(0, 4).text() == "CONNECTED"
    w.diag_page.manual.setText("com15")
    w.diag_page._use_manual()
    assert rig.cfg.manual_port == "COM15"


def test_first_run_defaults_to_production_and_never_creates_test_mode(tmp_path, monkeypatch):
    from hss.config import load_config
    from hss.firstrun import ensure_first_run
    monkeypatch.setenv("HSS_DATA_DIR", str(tmp_path))
    made = ensure_first_run()
    assert "config.json" not in made and "test_identities.json" not in made
    assert not load_config().test_mode
    assert not (tmp_path / "test_identities.json").exists()


def test_first_run_migrates_the_old_silently_created_test_config(tmp_path, monkeypatch):
    from hss.config import load_config
    from hss.firstrun import ensure_first_run
    monkeypatch.setenv("HSS_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text('{"_comment": "Default config (created on first run). x", "mode": "pin_test"}')
    ensure_first_run()
    assert not load_config().test_mode and (tmp_path / "config.json.auto-test-mode.bak").exists()
    # a deliberate config (no auto marker) is untouched and then gets the identity template
    (tmp_path / "config.json").write_text('{"mode": "pin_test"}')
    made = ensure_first_run()
    assert load_config().test_mode and "test_identities.json" in made
