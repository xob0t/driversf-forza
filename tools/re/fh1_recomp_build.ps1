# Builds Pinyon Shift (the FH1 recompilation, BSD-3, an external tool) from our FH1 disc by
# running its own build steps by hand, without editing any of its files. research/forza-recomp.md
# section 1.1. Its tools/build-preview.ps1 refuses any default.xex but the supported USA file
# (exact SHA-256 of the container: header, signature, encryption key). Ours is media 2B7A1346
# with the same code: xenia's module hash D48ABF1704CE5C4A (forza_recomp.py image). So this
# script runs the steps after that check: the same commands, in the same order.
#
#   powershell -File tools\re\fh1_recomp_build.ps1 -Root out\recomp\pinyon-v071 [-Parallel 2]
#
# Before it: the release tag's source in -Root, our disc extracted to <Root>\.local\game\base
# (forza_recomp.py extract-fh1), Visual Studio 2022 Build Tools with the C++ tools. Start it at
# Idle priority; its compilers inherit it. Shader preparation (prepare-fh1-shaders.ps1) is left
# out: it starts the game in a visible window.
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Root,
    [ValidateRange(1, 32)] [int]$Parallel = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path $Root).Path
$tools = Join-Path $Root 'tools'
. (Join-Path $tools 'release-common.ps1')  # their helpers (Enter-PinyonBuildEnvironment, ...)

# setup-preview.ps1's steps before extraction: pinned portable tools, the pinned ShiftGlue SDK.
& (Join-Path $tools 'provision-toolchain.ps1') | Out-Host
& (Join-Path $tools 'prepare-rexglue.ps1') | Out-Host

# build-preview.ps1, from after its default.xex hash check (as in v0.7.1).
$environment = Enter-PinyonBuildEnvironment
$sdkRoot = Resolve-PinyonRexGlueRoot
$generatedRoot = Resolve-PinyonLocalPath -RelativePath '.local/generated'
$rexglueExe = Join-Path $sdkRoot 'out/win-amd64/Release/rexglue.exe'
$manifest = Join-Path $Root 'config/rexglue/pinyon_shift_manifest.toml'
$logs = Resolve-PinyonLocalPath -RelativePath '.local/logs'
[void](New-Item -ItemType Directory -Force -Path $logs)
$env:SOURCE_DATE_EPOCH = '1784764800'
$cpu = Select-PinyonCpuBaseline -CpuBaseline 'auto'

Push-Location $sdkRoot
try {
    Invoke-PinyonBuildCommand $environment.CMake @('--preset', 'win-amd64',
        '-DREXGLUE_ENABLE_TRACY=OFF', '-DSDL_HIDAPI_LIBUSB=OFF') `
        (Join-Path $logs 'rexglue-configure.log') 'ReXGlue configuration failed.' -Step 'Configure the ReXGlue code generator'
    Invoke-PinyonBuildCommand $environment.CMake @('--build', '--preset', 'win-amd64-release',
        '--target', 'rexglue', '--parallel', "$Parallel") `
        (Join-Path $logs 'rexglue-build.log') 'ReXGlue code-generator build failed.' -Step 'Build the ReXGlue code generator'
}
finally { Pop-Location }
Invoke-PinyonBuildCommand 'mt.exe' @('-nologo', "-inputresource:$rexglueExe;#1",
    '-manifest', (Join-Path $Root 'config/rexglue/codegen-windows.manifest'),
    "-outputresource:$rexglueExe;#1") (Join-Path $logs 'rexglue-manifest.log') `
    'Could not enable Unicode paths for the code generator.' -Step 'Configure code generator Unicode paths'

# Translation, then their check that it logged exactly the warnings the supported image gives.
$codegenLog = Join-Path $logs 'codegen.log'
[void](New-Item -ItemType Directory -Force -Path $generatedRoot)
if (Test-Path -LiteralPath $codegenLog) { Remove-Item -LiteralPath $codegenLog -Force }
Invoke-PinyonLoggedCommand -FilePath $rexglueExe -Arguments @(
    '--log-level', 'info', '--log-file', $codegenLog, 'codegen', $manifest) `
    -LogPath (Join-Path $logs 'codegen-console.log') -Utf8Output | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Code generation failed ($LASTEXITCODE); see $logs\codegen-console.log." }
& (Join-Path $tools 'verify-codegen-log.ps1') -LogPath $codegenLog | Out-Host

Push-Location $Root
try {
    Invoke-PinyonBuildCommand $environment.CMake (@('--preset', 'win-amd64-release', "-DREXSDK_DIR=$sdkRoot") +
        $cpu.CMakeArguments + @("-DPYTHON_EXECUTABLE=$(Get-PinyonPython)")) `
        (Join-Path $logs 'preview-configure.log') 'Preview configuration failed.' -Step 'Configure the game build'
    Invoke-PinyonBuildCommand $environment.CMake @('--build', '--preset', 'win-amd64-release', '--parallel', "$Parallel") `
        (Join-Path $logs 'preview-build.log') 'Preview compilation failed.' -Step 'Compile the game'
}
finally { Pop-Location }
$exe = Join-Path $Root 'out/build/win-amd64-release/pinyon_shift.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'No pinyon_shift.exe was produced.' }
"built $exe"
