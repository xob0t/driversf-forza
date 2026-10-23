# Lists minidumps anywhere likely, plus open dialog windows, into C:\Output\find-dumps.txt.
$ErrorActionPreference = 'Continue'
& {
    '== dmp files'
    Get-ChildItem C:\Run, $env:TEMP, $env:LOCALAPPDATA\CrashDumps, ([Environment]::GetFolderPath('MyDocuments')) -Filter *.dmp -Recurse -ErrorAction SilentlyContinue |
        Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize | Out-String -Width 250
    '== Driver processes'
    Get-Process Driver -ErrorAction SilentlyContinue | Select-Object Id, MainWindowTitle, Responding | Format-Table | Out-String
    '== dbghelp'
    Test-Path C:\Windows\SysWOW64\dbghelp.dll
} *>&1 | Set-Content C:\Output\find-dumps.txt
