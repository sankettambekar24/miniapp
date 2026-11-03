<#
  Produce a ONE-FILE sketch for the Arduino IDE: the repo sketch with pins_local.h inlined.
  Output: firmware\single_file\hss_firmware_single\hss_firmware_single.ino  (git-ignored: it contains your PINs).
  Open that file in the IDE. Never commit it.
#>
$ErrorActionPreference = "Stop"
$fw = $PSScriptRoot
$ino = Get-Content (Join-Path $fw "hss_firmware\hss_firmware.ino") -Raw
$pins = Get-Content (Join-Path $fw "hss_firmware\pins_local.h") -Raw
if ($ino -notmatch '#include "pins_local.h"') { throw "repo sketch has no pins_local.h include" }
$out = Join-Path $fw "single_file\hss_firmware_single"
New-Item -ItemType Directory -Force $out | Out-Null
$single = $ino.Replace('#include "pins_local.h"', "// ---- inlined from pins_local.h (do not commit this file) ----`n" + $pins.TrimEnd())
Set-Content (Join-Path $out "hss_firmware_single.ino") $single -Encoding utf8
Write-Host "Wrote $out\hss_firmware_single.ino"
