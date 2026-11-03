"""The firmware's SHA-256 / HMAC / PBKDF2 (firmware/hss_firmware/hss_crypto.h) compiled on the PC and compared with Python
hashlib on many inputs. Status: MOCK-TESTED on the host CPU (same source as the board; the board itself is not run here).
Skipped if no g++ is available."""
import hashlib
import hmac
import shutil
import subprocess
from pathlib import Path

import pytest

FW = Path(__file__).resolve().parents[2] / "firmware" / "hss_firmware"
CPP = r'''
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "hss_crypto.h"
static std::vector<uint8_t> unhex(const char* s) { std::vector<uint8_t> v; for (size_t i = 0; s[i] && s[i+1]; i += 2) { char b[3] = {s[i], s[i+1], 0}; v.push_back((uint8_t)strtol(b, 0, 16)); } return v; }
static void hex(const uint8_t* d, size_t n) { for (size_t i = 0; i < n; i++) printf("%02x", d[i]); printf("\n"); }
int main(int argc, char** argv) {
  // argv: mode a b [c]
  std::string m = argv[1];
  uint8_t out[32];
  if (m == "sha") { auto a = unhex(argv[2]); hss_sha256(a.data(), a.size(), out); hex(out, 32); }
  else if (m == "hmac") { auto k = unhex(argv[2]); auto d = unhex(argv[3]); hss_hmac_sha256(k.data(), k.size(), d.data(), d.size(), out); hex(out, 32); }
  else if (m == "pbkdf2") { auto p = unhex(argv[2]); auto s = unhex(argv[3]); hss_pbkdf2_sha256(p.data(), p.size(), s.data(), s.size(), (uint32_t)atoi(argv[4]), out); hex(out, 32); }
  return 0;
}
'''


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    gpp = shutil.which("g++")
    if not gpp:
        pytest.skip("no g++ on this machine")
    d = tmp_path_factory.mktemp("crypto")
    (d / "t.cpp").write_text("#include <string>\n" + CPP)
    out = d / "t.exe"
    r = subprocess.run([gpp, "-O1", "-std=c++11", f"-I{FW}", str(d / "t.cpp"), "-o", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return str(out)


def run(exe, *args):
    return subprocess.run([exe, *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.parametrize("n", [0, 1, 3, 55, 56, 57, 63, 64, 65, 119, 120, 128, 1000])
def test_sha256_matches_hashlib_across_block_boundaries(exe, n):
    data = bytes((i * 7 + 3) & 0xFF for i in range(n))
    assert run(exe, "sha", data.hex() or "") == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("klen,mlen", [(4, 0), (4, 32), (4, 36), (6, 100), (64, 10), (65, 10), (100, 33)])
def test_hmac_matches_python(exe, klen, mlen):
    k, m = bytes(range(1, klen + 1)), bytes((i * 5) & 0xFF for i in range(mlen))
    assert run(exe, "hmac", k.hex(), m.hex() or "") == hmac.new(k, m, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("pin,iters", [("1234", 1), ("1234", 2), ("0000", 100), ("987654", 5000), ("12345678", 5000)])
def test_pbkdf2_matches_python(exe, pin, iters):
    salt = bytes(range(16))
    assert run(exe, "pbkdf2", pin.encode().hex(), salt.hex(), str(iters)) == hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iters, 32).hex()


def test_rfc7914_style_vector(exe):
    # RFC 6070 style check with SHA-256 (well-known vector: "password"/"salt", 1 and 2 iterations, 32 bytes)
    assert run(exe, "pbkdf2", b"password".hex(), b"salt".hex(), "1") == "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b"
    assert run(exe, "pbkdf2", b"password".hex(), b"salt".hex(), "2") == "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43"
