# firmware/
Working copy for Stage 3. NOTHING here is flashed or hardware-verified; every behavior is SKETCH-DERIVED (see `docs/SERIAL_PROTOCOL.md`).

- `hss_firmware/hss_firmware.ino` - sketch (base + HSS-S3 changes; compare `CHANGES_vs_BASE.diff`).
- `hss_firmware/pins_local.example.h` - template. `pins_local.h` (git-ignored) holds the two test PINs; `build.ps1` creates it with random PINs on first run.
- `build.ps1` - compile only (never flashes): `powershell firmware\build.ps1` (PIN-only, default) or `-Factors` (PIN -> face -> fingerprint).
- `BASE_SHA256.txt` - hash and origin of the base sketch (the base file itself is not committed: it contains a PIN literal).
- `build/` - outputs (git-ignored).
Backup gate: do not flash before `tests/hardware/FLASH_BACKUP.md` is done and confirmed. Flash steps: `tests/hardware/FIRMWARE_FLASH_AND_AUTH_TEST.md`.
