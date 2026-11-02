"""Dashboard tests on offscreen Qt. Status: MOCK-TESTED (fake serial, fake backend transport; no hardware, no network)."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QPushButton, QTabWidget

from hss.backend import BackendClient
from hss.backend.testing import FakeTransport
from hss.ui.dashboards import HW_BLOCKED, SIGN_IN_REASON, ListTab
from hss.ui.main_window import PAGE_ADMIN, PAGE_AUTH, PAGE_USER, MainWindow

from conftest import ST, Rig


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def login(rig, w, slot):
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says(f"AUTH_PIN_VERIFIED:{slot}")
    w.refresh()


def tab_titles(tabs: QTabWidget):
    return [tabs.tabText(i) for i in range(tabs.count())]


def test_admin_dashboard_tabs_and_role_from_identity_only(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    login(rig, w, 1)
    assert w.stack.currentIndex() == PAGE_ADMIN and w.dash.objectName() == "adminDashboard"
    assert tab_titles(w.dash) == ["Overview", "Physical SD", "Users", "Devices", "Files & permissions", "Audit",
                                  "Offline policies", "Backend"]


def test_user_dashboard_has_no_admin_tabs(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    login(rig, w, 2)
    assert w.stack.currentIndex() == PAGE_USER and w.dash.objectName() == "userDashboard"
    titles = tab_titles(w.dash)
    assert "Users" not in titles and "Audit" not in titles and "Devices" not in titles
    assert "My files (backend)" in titles and "Physical SD" in titles


def test_every_backend_control_disabled_with_reason_when_not_signed_in(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    login(rig, w, 1)
    checked = 0
    for i in range(w.dash.count()):
        t = w.dash.widget(i)
        if isinstance(t, ListTab):
            for b, a in t.btns:
                assert not b.isEnabled() and b.toolTip(), (w.dash.tabText(i), a.label)
                checked += 1
            assert not t.refresh_btn.isEnabled() and t.refresh_btn.toolTip() == SIGN_IN_REASON
            assert "No data" in t.status.text()
    assert checked >= 10


def test_blocked_backend_features_are_permanently_disabled_with_explanation(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    login(rig, w, 1)
    texts = {}
    for i in range(w.dash.count()):
        t = w.dash.widget(i)
        if isinstance(t, ListTab):
            for b, a in t.btns:
                if a.blocked:
                    texts[a.label] = b.toolTip()
                    assert not b.isEnabled()
    assert "BLOCKED" in texts["Download content"] and "503" in texts["Download content"]
    assert "BLOCKED" in texts["Issue policy"] and "503" in texts["Issue policy"]
    assert HW_BLOCKED.startswith("BLOCKED")


def test_sd_page_disabled_when_session_ends_and_dashboard_destroyed_on_unplug(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    login(rig, w, 1)
    sd = [w.dash.widget(i) for i in range(w.dash.count()) if w.dash.tabText(i) == "Physical SD"][0]
    sd.update_controls()
    assert sd.btn_list.isEnabled() and not sd.btn_down.isEnabled() and sd.btn_down.toolTip() == "Select a file first."
    rig.unplug()
    w.refresh()
    assert w.dash is None and w.stack.currentIndex() == PAGE_AUTH
    assert rig.ctl.backend is None and not rig.ctl.session.active
    sd.update_controls()
    assert not sd.btn_list.isEnabled() and "not connected" in sd.btn_list.toolTip().lower()


def test_backend_state_wiped_on_lock(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    login(rig, w, 1)
    c = rig.ctl.backend = BackendClient("https://example.invalid", rig.ctl.session, transport=FakeTransport())
    rig.ctl.session.backend_token = "memory-only-test-token"
    assert c.is_authenticated
    rig.device_says("AUTH_LOCKED")
    assert rig.ctl.backend is None
    assert rig.ctl.session.backend_token is None and not c.is_authenticated


def test_ensure_backend_rejects_insecure_url_and_none_without_url(app):
    rig = Rig([ST])
    assert rig.ctl.ensure_backend() is None
    with pytest.raises(ValueError):
        rig.ctl.ensure_backend("http://example.com")
    assert rig.ctl.ensure_backend("https://example.com").base_url == "https://example.com"


def test_no_enabled_button_without_an_action(app):
    rig = Rig([ST])
    w = MainWindow(rig.ctl)
    login(rig, w, 1)
    for b in w.dash.findChildren(QPushButton):
        if b.isEnabled():
            # enabled buttons must be wired (Qt exposes receivers via signal count)
            assert b.receivers("2clicked(bool)") > 0, b.text()
