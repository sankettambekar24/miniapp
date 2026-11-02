# FLASH_BACKUP - dump the currently flashed firmware BEFORE any flashing

Status: NOT-STARTED (user to perform). **Nothing in this project may flash the board until you confirm in writing that the backup exists in
`C:\mini project\HSS_Firmware_Backup\` and its SHA-256 hashes match `SHA256SUMS.txt`.** Claude does not touch that folder.

Why: the flashed build is not provably the loose sketch (`AUDIT.LOG` has `BOOT_OK`, which the sketch cannot emit; see `docs/CONFLICTS.md`).

## 0. Read this first - protection warning
STM32 Read-Out Protection (RDP): `0xAA` = Level 0 (unprotected), `0xBB` = Level 1 (flash unreadable over SWD), `0xCC` = Level 2 (permanent).
**NEVER set RDP back to Level 0 to "make it readable": that triggers a mass erase and DESTROYS the firmware you want to save.**
If RDP is Level 1 the dump is not possible; stop, record it in `notes.txt`, and tell Claude. The original `.ino` is then your only source.

## 1. Hardware
1. Power the board from USB or the ST-Link 3.3 V, not both at once if unsure.
2. Wire ST-Link to the board SWD pins: SWDIO, SWCLK, GND, 3V3 (optionally NRST). The Black Pill has a 4-pin SWD header.
3. Plug in the ST-Link. Close the HSS app, Arduino IDE serial monitor and any old HSS agent/bridge (`Get-Process python*`).

## 2. GUI method (STM32CubeProgrammer)
1. Interface = ST-LINK, Mode = Normal (or "Hot plug" if Normal fails), Reset mode = Software reset. Click **Connect**.
2. Note the Device panel: device name, **Device ID** (expected 0x423 or 0x433 for F401, 0x413 for F405/F407 - from memory, confirm against ST docs), flash size.
3. Open **OB (Option Bytes)** tab > *Read Out Protection*. Record the value (AA / BB / CC). Do NOT change anything.
4. If RDP = AA: **Device memory** tab > Address `0x08000000`, Size = full flash size (e.g. `0x40000` for 256 KB, `0x80000` for 512 KB) > **Read** > **Save as** `firmware_dump.bin`.

## 3. CLI method (same result; paste the output)
```
STM32_Programmer_CLI -c port=SWD mode=UR -ob displ
STM32_Programmer_CLI -c port=SWD mode=UR -r 0x08000000 0x40000 firmware_dump.bin
```
(`mode=UR` = connect under reset; replace `0x40000` with your flash size from the device info.)
`-ob displ` prints the RDP byte. Save its output as `ob.txt`.

## 4. Save and hash (you do this; Claude never writes to the backup folder)
Place files exactly here:
```
C:\mini project\HSS_Firmware_Backup\
  original_sketch\   (all .ino incl. the WhatsApp copy)
  flash_dump\        firmware_dump.bin, ob.txt
  notes.txt          board model, dump date, tool + version, RDP value, flash size, device ID
  SHA256SUMS.txt
```
Create hashes (PowerShell, run from inside the backup folder):
```
Get-ChildItem -Recurse -File | Where-Object Name -ne 'SHA256SUMS.txt' | ForEach-Object {
  "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash, $_.FullName.Substring((Get-Location).Path.Length+1) } | Set-Content SHA256SUMS.txt
```
Sanity checks: dump size equals flash size; first 4 bytes at offset 0 are the initial stack pointer (RAM address `20xx xxxx`, little-endian) and bytes 4-7 the reset vector (`08xx xxxx`).
Then store a second copy off this PC (USB stick/cloud). Reply "BACKUP CONFIRMED" and paste `notes.txt`.

## 5. Evidence
Paste CubeProgrammer connect log / CLI output for the log into `tests/evidence/hardware/flash_backup/` (no personal data).
