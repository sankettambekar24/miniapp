# TOOLCHAIN (probed 2026-11-03, this machine)

| Tool | Status | Detail |
|---|---|---|
| Python | INSTALLED (3 versions) | 3.14.6 (`C:\Python314`, default `python`), 3.13 and 3.11.9 (`%LOCALAPPDATA%\Programs\Python\Python3xx`) |
| PySide6 | MISSING | not in 3.14 (import fails); 3.11/3.13 pip lists show none |
| pyserial | INSTALLED (3.14 only) | 3.5 |
| PyInstaller | MISSING | not on PATH, not in pip lists |
| pytest / requests / httpx | MISSING | not in any pip list |
| Inno Setup 6 | MISSING | `ISCC.exe` not at default path; NSIS/WiX also absent |
| signtool | INSTALLED | `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe` (no code-signing certificate exists; unsigned build will trigger SmartScreen) |
| git | INSTALLED | 2.53.0.windows.3 |
| winget | INSTALLED | `Get-Command winget` resolved |
| Arduino CLI / IDE | PARTIAL | `%LOCALAPPDATA%\Arduino15` has only the **AVR** core + stock libs (SD, Servo, ...). **No STM32 core** (`STMicroelectronics:stm32`), no `arduino-cli` on PATH |
| STM32CubeIDE / CubeProgrammer | MISSING | `C:\Program Files\STMicroelectronics` absent; `STM32_Programmer_CLI` absent |
| PlatformIO | MISSING | no `pio` |
| MSVC Build Tools | INSTALLER ONLY | `C:\mini project\vs_BuildTools.exe` present, install state not verified |

## Recommended versions and install commands (not yet run)
- **Python 3.11.9** for the app (PySide6 and PyInstaller wheels are mature there; 3.14 is too new for some binary wheels, UNVERIFIED): `py -3.11 -m venv .venv` then `pip install PySide6 pyserial pytest pyinstaller`.
- **Inno Setup 6**: `winget install JRSoftware.InnoSetup`.
- **Firmware build**: `winget install ArduinoSA.CLI`, then `arduino-cli core install STMicroelectronics:stm32` (board package URL must be added to config). Flashing needs STM32CubeProgrammer: `winget install STMicroelectronics.STM32CubeProgrammer`. Exact board/variant is UNKNOWN until firmware source or board evidence is found (see FIRMWARE_ANALYSIS.md).
- **Signing**: needs a purchased code-signing certificate; `signtool` is already present.

## Update 2026-11-03 (Stage 2)
Project venv created at `HSS_Complete_Desktop\.venv` (Python 3.11.9) with: PySide6 6.12.0, pyserial 3.5, pytest 9.2.0, pytest-qt 4.5.0, PyInstaller 6.22.3.
`.venv` is git-ignored. Recreate: `py -3.11 -m venv .venv` then `.venv\Scripts\pip install -r requirements-dev.txt`.

### SYSTEM installs - NOT RUN, awaiting your approval
Exact commands (run only after you approve; each needs no code changes in this repo):
```
winget install --id JRSoftware.InnoSetup -e                  # Inno Setup 6 (Stage 6 installer)
winget install --id STMicroelectronics.STM32CubeProgrammer -e # firmware dump/flash (needed for tests/hardware/FLASH_BACKUP.md)
winget install --id ArduinoSA.CLI -e                          # arduino-cli (Stage 3 compile); then:
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://github.com/stm32duino/BoardManagerFiles/raw/main/package_stmicroelectronics_index.json
arduino-cli core update-index
arduino-cli core install STMicroelectronics:stm32
# alternative to arduino-cli: PlatformIO  ->  pip install platformio   (inside a venv)
```
Winget package IDs above are from memory and UNVERIFIED; confirm with `winget search <name>` before running.
