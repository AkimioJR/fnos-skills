$Architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$Platform = if ($Architecture -eq "Arm64") { "windows-arm64" } else { "windows-x64" }
$Target = Join-Path $PSScriptRoot "..\bin\trim-cli-$Platform.exe"

$SkipValue = $false
foreach ($Argument in $args) {
  if ($SkipValue) {
    $SkipValue = $false
    continue
  }
  if ($Argument -in @("--host", "--port", "--profile", "--scheme")) {
    $SkipValue = $true
    continue
  }
  if ($Argument.StartsWith("-")) {
    continue
  }
  if ($Argument -eq "media") {
    Write-Error "Media commands are not available in this skill package."
    exit 2
  }
  break
}

if (-not (Test-Path $Target)) {
  Write-Error "Missing packaged binary: $Target"
  exit 1
}

& $Target @args
exit $LASTEXITCODE
