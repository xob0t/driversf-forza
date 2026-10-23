# Runs as the sandbox user. Copies the host's Driver profile (save and
# settings) from the read-only C:\Profile mapping and switches the guest copy
# to a 960x540 window, which the sandbox's virtual GPU handles reliably.
$ErrorActionPreference = 'Stop'
$source = 'C:\Profile'
if (-not (Test-Path (Join-Path $source 'reloaded\savegames\autosave.dat'))) {
    throw 'C:\Profile has no reloaded\savegames\autosave.dat; refusing to start a new game.'
}
$target = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Driver San Francisco'
Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item (Join-Path $source '*') $target -Recurse -Force

$graphics = Join-Path $target 'graphics.ini'
(Get-Content $graphics) `
    -replace '^Fullscreen\s*=.*$', 'Fullscreen = 0' `
    -replace '^Width\s*=.*$', 'Width = 960' `
    -replace '^Height\s*=.*$', 'Height = 540' | Set-Content $graphics

@{ profile = $target; utc = (Get-Date).ToUniversalTime().ToString('o') } |
    ConvertTo-Json | Set-Content C:\Output\setup-user.json
