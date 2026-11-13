param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("part-a-b", "part-c-rr", "part-d-wrr", "part-d-lr", "part-d-hc")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "envoy\envoy-$Mode.yaml"
$dst = Join-Path $PSScriptRoot "envoy\envoy.yaml"

Copy-Item -Path $src -Destination $dst -Force
Write-Host "Copied envoy-$Mode.yaml -> envoy.yaml"

docker restart bookstore-envoy | Out-Null
Start-Sleep -Seconds 3
Write-Host "Restarted bookstore-envoy with mode: $Mode"

