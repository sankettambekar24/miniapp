"""SD validation rules (pure functions). Status: MOCK-TESTED."""
import pytest

from hss.core.sd_ops import (MAX_PATH_LEN, SdError, SdErrorCode, check_remote_path, check_upload_name,
                             map_device_error, validate_remote_path, validate_upload_name)


@pytest.mark.parametrize("name", ["a.txt", "report 1.pdf", "x", "a" * 120, "UPPER.BIN", "con_x.txt", "COM10.txt", "a.b.c"])
def test_good_upload_names(name):
    assert check_upload_name(name) is None


@pytest.mark.parametrize("name", [
    "", "a/b.txt", "a\b.txt", "c:x.txt", "..", ".", "a..b", "../x", "a" * 121, "bad\x00name", "tab\tname", "new\nline",
    "CON", "con.txt", "PRN", "Aux.log", "NUL.", "COM1", "lpt9.dat", "trail.", "trail ", " lead", "q?.txt", "s*.txt",
    "a|b", 'q"x', "a<b", "a>b", "caf\u00e9.txt", "del\x7f"])
def test_bad_upload_names(name):
    assert check_upload_name(name) is not None
    with pytest.raises(SdError) as e:
        validate_upload_name(name)
    assert e.value.code is SdErrorCode.VALIDATION


@pytest.mark.parametrize("path", ["a.txt", "dir/a.txt", "a/b/c/d.bin", "x" * MAX_PATH_LEN, "with space/f"])
def test_good_remote_paths(path):
    assert validate_remote_path(path) == path


@pytest.mark.parametrize("path", [
    "", "/abs", "\abs", "a\b", "c:/x", "a//b", "a/", "./a", "a/./b", "../a", "a/../b", "a/..", "x" * (MAX_PATH_LEN + 1),
    "ctl\x01", "caf\u00e9", "a/\x1f"])
def test_bad_remote_paths(path):
    assert check_remote_path(path) is not None
    with pytest.raises(SdError):
        validate_remote_path(path)


def test_device_error_mapping():
    assert map_device_error("DESTINATION_EXISTS").code is SdErrorCode.COLLISION
    assert map_device_error("AUTH_REQUIRED").code is SdErrorCode.NOT_AUTHENTICATED
    assert map_device_error("LOCKED_OUT").code is SdErrorCode.LOCKED_OUT
    assert map_device_error("FILE_TOO_LARGE").code is SdErrorCode.TOO_LARGE
    assert map_device_error("VERIFY_SIZE_MISMATCH").code is SdErrorCode.SIZE_MISMATCH
    e = map_device_error("CARD_UNAVAILABLE")
    assert e.code is SdErrorCode.DEVICE_ERROR and e.device_code == "CARD_UNAVAILABLE"
