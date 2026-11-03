<#
  Build the HSS firmware with the project-local Arduino CLI (tools\arduino-cli, STM32duino core).
  Default: PIN-only build (HSS_REQUIRE_FACTORS=0). Use -Factors for the THREE-FACTOR build (fingerprint -> face -> per-user PIN; docs/THREE_FACTOR_DESIGN.md).
  Outputs: build\pinonly\ or build\factors\ (never mixed).

  First run creates firmware\hss_firmware\pins_local.h (git-ignored) with two random distinct 4-digit test PINs.
  The PINs are NOT printed; read them from that file on your own machine (slot 1 = admin test, slot 2 = user test)
  or edit the file and rebuild. NEVER commit pins_local.h.

  This script only compiles. It never flashes.
#>
param(
  [switch]$Factors,
  [string]$Fqbn = "STMicroelectronics:stm32:GenF4:pnum=BLACKPILL_F401CC,usb=CDCgen,xserial=generic"
)
$ErrorActionPreference = "Stop"
$root  = Split-Path -Parent $PSScriptRoot
$cli   = Join-Path $root "tools\arduino-cli\arduino-cli.exe"
$cfg   = Join-Path $root "tools\arduino-data\arduino-cli.yaml"
$sketch = Join-Path $PSScriptRoot "hss_firmware"
$out   = Join-Path $PSScriptRoot $(if ($Factors) { "build\factors" } else { "build\pinonly" })
$pins  = Join-Path $sketch "pins_local.h"

if (-not (Test-Path $cli)) { throw "arduino-cli not found at $cli (see docs/TOOLCHAIN.md)" }
if (-not (Test-Path $pins)) {
  $rng = New-Object System.Random
  do { $p1 = $rng.Next(1000, 9999); $p2 = $rng.Next(1000, 9999) } while ($p1 -eq $p2)
  "// Local test PINs (git-ignored). Slot 1 = admin test, slot 2 = user test.`n#define HSS_PIN_SLOT1 `"$p1`"`n#define HSS_PIN_SLOT2 `"$p2`"" |
    Set-Content $pins -Encoding ascii
  Write-Host "Created $pins with random test PINs (not printed). Read them from that file."
}

# -DENABLE_HWSERIAL2 asks the STM32duino core (3.0.0, cores/arduino/WSerial.h) to provide USART2 as `Serial2` with the core's DEFAULT pins.
# The base sketch uses Serial2 without setting pins (FIRMWARE_ANALYSIS.md s2), so no pin is invented here;
# which physical pins that is on your board is UNVERIFIED (hardware test step).
$flags = "-DENABLE_HWSERIAL2 " + $(if ($Factors) { "-DHSS_REQUIRE_FACTORS=1" } else { "-DHSS_REQUIRE_FACTORS=0" })
New-Item -ItemType Directory -Force $out | Out-Null
Write-Host "FQBN : $Fqbn"
Write-Host "Build: $(if ($Factors) {'THREE-FACTOR fingerprint -> face -> PIN (HSS_REQUIRE_FACTORS=1)'} else {'PIN-only (HSS_REQUIRE_FACTORS=0)'})"
& $cli --config-file $cfg compile --clean --fqbn $Fqbn --build-property "compiler.cpp.extra_flags=$flags" --output-dir $out $sketch
if ($LASTEXITCODE -ne 0) { throw "compile failed (exit $LASTEXITCODE)" }
$bin = Join-Path $out "hss_firmware.ino.bin"
if (Test-Path $bin) { "{0}  {1}  ({2} bytes)" -f (Get-FileHash $bin -Algorithm SHA256).Hash, $bin, (Get-Item $bin).Length }
