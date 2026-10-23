<#
Frame rate of the running game inside the test VM: waits for Driver.exe, waits -DelaySeconds
more (into the world), records -Seconds of presents with Intel PresentMon (ETW, needs the
admin user the VM logs in as) and writes <Out>.csv and <Out>.json (mean and median fps,
1 % low). Run next to a test, e.g.
  .\tools\wsb\vm.ps1 Exec -Script C:\Repo\tools\wsb\guest\fps.ps1 -Arguments '-DelaySeconds','60'
PresentMon (PresentMon-<ver>-x64.exe, signed by Intel) is expected in C:\Output\vm\.
#>
param(
    [int]$Seconds = 30,
    [int]$DelaySeconds = 60,
    [int]$WaitSeconds = 600,
    [string]$Out = "C:\Output\vm\fps-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
)
$ErrorActionPreference = 'Stop'
$exe = Get-ChildItem 'C:\Output\vm' -Filter 'PresentMon*.exe' | Select-Object -First 1
if (-not $exe) { throw 'no PresentMon*.exe in C:\Output\vm' }
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while (-not (Get-Process Driver -ErrorAction SilentlyContinue)) {
    if ((Get-Date) -gt $deadline) { throw "Driver.exe not running after $WaitSeconds s" }
    Start-Sleep -Seconds 2
}
Start-Sleep -Seconds $DelaySeconds
$csv = "C:\dsf\fps.csv"   # local first: PresentMon writes many small lines
Remove-Item $csv -ErrorAction SilentlyContinue
& $exe.FullName --process_name Driver.exe --output_file $csv --timed $Seconds --terminate_after_timed `
    --no_console_stats --stop_existing_session --session_name dsfforza_fps *> "$Out.log"
if (-not (Test-Path $csv)) { throw "PresentMon wrote no CSV (exit $LASTEXITCODE; see $Out.log)" }
$rows = @(Import-Csv $csv)
$column = @('MsBetweenPresents', 'FrameTime', 'msBetweenPresents') | Where-Object { $rows.Count -and $rows[0].PSObject.Properties.Name -contains $_ } | Select-Object -First 1
$ms = @($rows | ForEach-Object { [double]::Parse($_.$column, [Globalization.CultureInfo]::InvariantCulture) } | Where-Object { $_ -gt 0 } | Sort-Object)
$summary = [ordered]@{ seconds = $Seconds; frames = $ms.Count; column = $column }
if ($ms.Count) {
    $summary.meanFps = [math]::Round(1000 / (($ms | Measure-Object -Average).Average), 1)
    $summary.medianFps = [math]::Round(1000 / $ms[[int]($ms.Count / 2)], 1)
    $summary.low1Fps = [math]::Round(1000 / $ms[[int]([math]::Min($ms.Count - 1, $ms.Count * 0.99))], 1)
    $summary.presentMode = ($rows | Group-Object PresentMode | Sort-Object Count -Descending | Select-Object -First 1).Name
}
Copy-Item $csv "$Out.csv"
$summary | ConvertTo-Json | Set-Content "$Out.json"
