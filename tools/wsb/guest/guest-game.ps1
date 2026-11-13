# Which parts of the host's game folder a test guest gets (dot-sourced by setup-vm.ps1 -Phase Game,
# run-test.ps1 outside -OnHost, and run-series.ps1, which compares the guest's root files with the
# host's). research/in-game-tests.md, "Guest game folder".
#
# The guest runs the game as the retail 1.04 folder has it plus dsf_enhanced; the owner's other setup
# stays on the host (issue #589):
#   - Folders: the retail game's own (the ones whose files date from the 2012 release: bin, Common,
#     Input, Install, Locale, media) and plugins, which run-test.ps1 fills itself (DriverNGHook,
#     -Asi). Not mods (Felony Rework, the mod menu), not dsf_enhanced's redirect folder and the world
#     map it points at (worldlod-map, 0.8 GB more world, GPU memory the VM doesn't have), not the
#     DLSS 5 helper or its dumps, not an installed dsfcars\ (its car packages would load in every -Asi run).
#   - Root files: all but graphics API proxies (Windows loads these from the game folder before
#     System32, so DXVK's d3d9.dll there replaces the guest's Direct3D 9 with Vulkan, which the VMs
#     don't have: the game exits at start with 0xC0000409), backups, logs and dumps.

$GuestGameFolders = 'bin', 'Common', 'Input', 'Install', 'Locale', 'media', 'plugins'
$GuestGameSkipFiles = 'd3d8.dll', 'd3d9.dll', 'd3d10.dll', 'd3d10_1.dll', 'd3d10core.dll', 'd3d11.dll', 'd3d12.dll',
    'dxgi.dll', 'ddraw.dll', 'opengl32.dll', 'vulkan-1.dll', 'dxvk.conf', '*.bak', '*.bak-*', '*.log', '*.dmp'

function Test-GuestGameFile([string]$Name) {
    # A root file of the game folder the guest gets.
    foreach ($pattern in $GuestGameSkipFiles) { if ($Name -like $pattern) { return $false } }
    return $true
}

function Test-GuestGameFolder([string]$Name) {
    # A top-level folder of the game folder the guest gets.
    return $GuestGameFolders -contains $Name
}

function Get-RunEnhancedIni([string[]]$Lines, [switch]$Guest) {
    # run-test.ps1's copy of dsf_enhanced.ini: traffic spawn distance and engine LOD thresholds at the
    # game's own (1.0). -Guest (a VM or sandbox run) also turns DLSS 5 and DLAA off: they need an NVIDIA
    # RTX GPU and the helper in the game's dlss5 folder, which a guest has neither of. With DLAA on and no
    # helper, VM shots came out black while the Lua drove on (issue #589). Keys are matched per section
    # ([Ultrawide] has its own Enabled).
    $off = if ($Guest) { @{ 'DLSS5' = @('Export', 'Helper'); 'DLAA' = @('Enable') } } else { @{} }
    $section = ''
    foreach ($line in $Lines) {
        if ($line -match '^\s*\[([^\]]+)\]') { $section = $Matches[1] }
        $key = if ($line -match '^\s*([A-Za-z0-9_]+)\s*=') { $Matches[1] } else { '' }
        if ($key -in 'TrafficSpawnDistanceScale', 'LODEngineThresholdScale') { "$key=1.0" }
        elseif ($key -and $off[$section] -contains $key) { "$key=0" }
        else { $line }
    }
}
