# ARCHITECTURE (PROPOSED, not built; status NOT-STARTED)

Stack: Python 3.11, PySide6, pyserial, PyInstaller one-folder, Inno Setup. Data in `%LOCALAPPDATA%\HSS`.

```
hss/
  ui/            PySide6 shell: sidebar, status bar (USB state | auth mode | backend), auth screen, admin/user dashboards
  core/
    serial_manager.py   SINGLE owner of the COM port; worker thread; command queue (request IDs, timeouts)
    discovery.py        USB descriptor match (VID/PID/description from config); hot-plug; never hardcoded COM
    auth_fsm.py         IDLE > WAITING_PIN > VERIFIED|FAILED|LOCKED > SESSION_ACTIVE > LOCKED_ON_DISCONNECT
    identity.py         PIN-slot -> identity/role (local, non-bundled file); firmware never picks role
    sd_ops.py           list/upload/download via SerialManager only; path/size validation, chunk ack/retry/abort
    protocol.py         shared protocol constants (also parsed by conformance test against firmware strings)
  backend/       api client, memory-only JWT, redacted logging, cold-start retry (Stage 4)
  factors/       face + fingerprint: interface + disabled stub only
  config.py      mode flag (production | pin_test), VID/PID list, backend URL
firmware/        working copy of the .ino (Stage 5)
tests/ (unit, mock serial, contract, hardware/, release/)
```

Rules: serial I/O only in SerialManager; UI talks to it via Qt signals; disconnect = immediate lock + clear session/token state;
messages not tied to the current auth session are dropped; production mode fail-closed.
Known constraints from discovery: firmware lockout only clears on reset (BLOCKS "retry after lockout" UX, show "power-cycle device");
firmware has no framing/CRC; face result is a forgeable host assertion; backend has no trusted hardware-verified session issuance.
