# PIN_AUTH_TEST - Stage 2 hardware test script

Current status of every step: **MOCK-TESTED** (desktop logic, fake serial) and **NOT-STARTED on hardware**.
Nothing becomes VERIFIED-HARDWARE until you paste logs. Steps marked **BLOCKED-ON-FIRMWARE** cannot pass with the current firmware by design.
The board runs whatever is flashed now (do not flash anything for this test). Never paste your PIN.

Evidence rule: save logs/screenshots in `C:\mini project\HSS_Complete_Desktop\tests\evidence\hardware\pin_auth\` and paste them to Claude.

## Setup
1. Close the Arduino serial monitor, STM32CubeProgrammer and any old HSS agent: `Get-Process python*,*Hybrid* -ErrorAction SilentlyContinue` (stop old `usb_watch_agent`/`face_bridge`/`HybridSecureStorageDesktop` if present - they would hold the COM port).
2. Create user data (examples contain no real data):
```
$d="$env:LOCALAPPDATA\HSS"; New-Item -ItemType Directory -Force $d | Out-Null
Copy-Item "C:\mini project\HSS_Complete_Desktop\config\config.example.json" "$d\config.json"
Copy-Item "C:\mini project\HSS_Complete_Desktop\config\test_identities.example.json" "$d\test_identities.json"
```
3. Start the app with no console window: `& "C:\mini project\HSS_Complete_Desktop\.venv\Scripts\pythonw.exe" -m hss` (run from `C:\mini project\HSS_Complete_Desktop`).
   Debug alternative with a console: `.venv\Scripts\python.exe -m hss`.

## Steps
| # | Action | Expected | Result (fill in) |
|---|---|---|---|
| 1 | Board UNPLUGGED. Run: `Get-PnpDevice -Class Ports \| Select FriendlyName,InstanceId`. Then plug the board in and run it again. Paste both. | New entry for the board; InstanceId contains `VID_0483&PID_5740` (assumption from old code; if it differs, tell Claude the VID/PID and edit `usb_ids` in `config.json`). | |
| 2 | With the board unplugged, start the app. Screenshot. | Red banner `PIN-ONLY TEST MODE - not production authentication`; title `PIN-ONLY TEST MODE`; status bar `USB: no HSS device found`; Face/Fingerprint shown greyed `Not available (disabled stub)`; Start button disabled; no console window. | |
| 3 | Plug in the board. | Within ~2 s status bar `USB: connected (COMx)` (port discovered, not hardcoded); Start button enabled. Note COMx. | |
| 4 | Click **Start authentication**. | State `WAITING_PIN: Enter your PIN on the device keypad`. | |
| 5 | Enter a WRONG PIN on the device keypad, press `#`. | Device log shows `DBG: EVENT PIN_FAILED` (current firmware, `.ino` L958). State `FAILED: PIN rejected (1/3)...`. | |
| 6 | Click Start again, enter the CORRECT PIN, press `#`. | Device log shows `DBG: EVENT PIN_VERIFIED`. State `FAILED` with message containing `BLOCKED-ON-FIRMWARE` (legacy firmware reports no PIN slot, so the app fails closed and never creates a session). Device then logs `FACE_SCAN`; ignore it (face is a disabled stub; the device will time out the face step after ~30 s). | |
| 7 | Slot test: PIN slot 1 shows the admin dashboard page, slot 2 the user dashboard page, with no role selector. | **BLOCKED-ON-FIRMWARE** (needs Stage 3 firmware emitting `AUTH_PIN_VERIFIED:<slot>`). Skip. | BLOCKED |
| 8 | Lockout: Start, then enter 3 WRONG PINs (restart auth after each). | After the 3rd, device log `DBG: SYSTEM LOCKED`; state `LOCKED`; Start disabled (tooltip). Documented behaviour: lockout clears only by device reset/power-cycle. | |
| 9 | Unplug the USB cable (do this during `WAITING_PIN` and, if you get a slot-1/2 firmware later, during a session). | Status changes IMMEDIATELY to `LOCKED_ON_DISCONNECT`, status bar `USB: disconnected`, any dashboard page closes. | |
| 10 | Re-plug. | Status stays `LOCKED_ON_DISCONNECT` ("re-authentication required") until you click Start; port rediscovered (COMx may differ). | |
| 11 | Close the app. Copy `%LOCALAPPDATA%\HSS\logs\hss.log` to the evidence folder and paste it (it contains device status lines only, never the PIN). | Log has `connected ... gen=1`, auth transitions. | |
| 12 | Optional: plug in a second matching device, if you have one. | `USB: multiple matching devices - unplug extras`; app refuses to connect (fail-closed). | |

## Caveat about the device failure counter (legacy firmware)
The sketch counts PIN failures AND face/fingerprint denials toward the 3-strike lockout (`docs/FIRMWARE_ANALYSIS.md` s4; `.ino` L970/L978), and the counter lives in RAM. In step 6 the face step is not answered by the desktop, so it may time out and count as a failure. If the device locks earlier than you expect, that is why: re-plug/reset the board to clear it (reset clears the counter), then continue. Do step 8 last or start it from a fresh reset.

## Report back
Paste for each step: PASS / FAIL / BLOCKED plus the log/screenshot. Claude will then update labels (VERIFIED-HARDWARE only for steps with pasted evidence).
