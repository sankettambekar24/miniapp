<#
  Build the frozen app and (if Inno Setup is available) the installer. Runs the frozen smoke test.
  Output: dist\HSS\HSS.exe, release\HSS-Setup.exe (UNSIGNED; labelled unverified-on-other-PCs).
  Never touches hardware, the firmware backup folder, or the network.
#>
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
$ver = (& $py -c "import hss;print(hss.__version__.split('-')[0])").Trim()

Remove-Item -Recurse -Force build\pyi, dist -ErrorAction SilentlyContinue
& $py -m PyInstaller hss.spec --noconfirm --distpath dist --workpath build\pyi
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Frozen smoke test: isolated data dir, offscreen Qt, no serial port opened.
$tmp = Join-Path $env:TEMP ("hss_smoke_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory $tmp | Out-Null
$env:HSS_DATA_DIR = $tmp; $env:QT_QPA_PLATFORM = "offscreen"
$p = Start-Process ".\dist\HSS\HSS.exe" -ArgumentList "--smoke","--smoke-out","$tmp\smoke.json" -Wait -PassThru
Remove-Item Env:HSS_DATA_DIR, Env:QT_QPA_PLATFORM
if ($p.ExitCode -ne 0 -or -not (Test-Path "$tmp\smoke.json")) { throw "frozen smoke test FAILED (exit $($p.ExitCode))" }
Get-Content "$tmp\smoke.json"

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "C:\Program Files (x86)\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
  & $iscc "/DAppVersion=$ver" installer\HSS.iss
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
  Get-FileHash release\HSS-Setup.exe -Algorithm SHA256 | Format-List Hash, Path
} else { Write-Warning "Inno Setup not found; installer skipped." }
