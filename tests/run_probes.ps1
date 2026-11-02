# READ-ONLY probe: GET/HEAD/OPTIONS only.
param([string]$Base="https://mini-backend-z8dr.onrender.com", [string]$Out="C:\mini project\HSS_Complete_Desktop\tests\evidence\probes")
$nil="00000000-0000-0000-0000-000000000000"
$probes=@(
 @("GET","/api/v1/health/live/"),@("HEAD","/api/v1/health/live/"),@("OPTIONS","/api/v1/health/live/"),
 @("GET","/api/v1/health/ready/"),
 @("GET","/api/v1/auth/me/"),
 @("GET","/api/v1/auth/token/"),@("OPTIONS","/api/v1/auth/token/"),
 @("GET","/api/v1/auth/preauth/"),@("GET","/api/v1/auth/register/"),@("GET","/api/v1/auth/logout/"),
 @("GET","/api/v1/auth/token/refresh/"),@("GET","/api/v1/auth/change-password/"),
 @("GET","/api/v1/devices/"),@("GET","/api/v1/devices/$nil/"),
 @("GET","/api/v1/auth-attempts/$nil/"),@("GET","/api/v1/auth-attempts/"),
 @("GET","/api/v1/files/"),@("GET","/api/v1/files/$nil/"),@("GET","/api/v1/files/$nil/content/"),@("GET","/api/v1/files/$nil/grants/"),
 @("GET","/api/v1/offline-policies/"),@("GET","/api/v1/offline-policies/$nil/"),
 @("GET","/api/v1/audit/events/"),@("GET","/api/v1/admin/users/"),
 @("GET","/api/v1/"),@("GET","/"),
 @("GET","/api/schema/"),@("GET","/api/docs/"),@("GET","/openapi.json"),@("GET","/swagger/"),@("GET","/docs"),@("GET","/redoc/"),
 @("GET","/admin/login/")
)
$keep='content-type|www-authenticate|allow|x-request-id|retry-after|cache-control|access-control|server|x-frame-options|strict-transport-security|vary|x-content-type-options|location|cf-ray|rndr'
$first=$true
foreach($p in $probes){
  $m=$p[0];$path=$p[1];$url="$Base$path"
  $slug=($m+"_"+$path.Trim('/').Replace('/','_').Replace($nil,'NILUUID')); if($slug -eq "${m}_"){$slug="${m}_root"}
  $bodyf=[IO.Path]::GetTempFileName(); $hdrf=[IO.Path]::GetTempFileName()
  $code="000"; $tries= if($first){3}else{1}; $mt= if($first){90}else{60}
  for($i=0;$i -lt $tries;$i++){
    $args=@("-s","-m",$mt,"-o",$bodyf,"-D",$hdrf,"-w","%{http_code}","-X",$m,$url)
    if($m -eq "HEAD"){$args=@("-s","-m",$mt,"-I","-D",$hdrf,"-o",$bodyf,"-w","%{http_code}",$url)}
    $code=& curl.exe @args
    if($code -ne "000"){break}
  }
  $first=$false
  $hdrs=[ordered]@{}
  foreach($l in (Get-Content $hdrf)){ $ix=$l.IndexOf(':'); if($ix -gt 0){ $k=$l.Substring(0,$ix).Trim().ToLower(); if($k -match $keep){$hdrs[$k]=$l.Substring($ix+1).Trim()} } }
  $body=""; if(Test-Path $bodyf){$body=[IO.File]::ReadAllText($bodyf)}
  if($body -and $body.Length -gt 4000){$body=$body.Substring(0,4000)+"...[truncated]"}
  $obj=[ordered]@{timestamp=(Get-Date).ToUniversalTime().ToString("o");method=$m;url=$url;status=[int]$code;headers=$hdrs;body=$body}
  $obj | ConvertTo-Json -Depth 5 | Set-Content -Path "$Out\$slug.json" -Encoding UTF8
  "$m $path -> $code"
  Remove-Item $bodyf,$hdrf -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 4
}
