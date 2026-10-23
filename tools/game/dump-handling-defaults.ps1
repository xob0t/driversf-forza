<#
.SYNOPSIS
Reads the 607 handling-parameter defaults from a running Driver.exe.

.DESCRIPTION
VEdit.GetGameDefaults (0x00626F00) walks 607 parameters: types at 0x00F3E450
(0 float, 1 bool, 2 int, else unused) and values at 0x0144FF0C. The values
are in .bss, filled in at startup, so they can only be read from a running
game. Driver.exe has a fixed image base, so these addresses are absolute.

Read-only: opens the process with PROCESS_VM_READ and writes a TSV.

.EXAMPLE
.\tools\game\dump-handling-defaults.ps1   # with the game at the main menu or in game
#>
param([string]$Out = (Join-Path $PSScriptRoot '..\..\research\handling-defaults.tsv'))

$ErrorActionPreference = 'Stop'
$count = 0x25F
$typesVa = 0x00F3E450
$valuesVa = 0x0144FF0C

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DsfMemory {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(int access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] buffer, int size, out int read);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    public static byte[] Read(int pid, long address, int size) {
        IntPtr process = OpenProcess(0x0010 /* PROCESS_VM_READ */, false, pid);
        if (process == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
        try {
            byte[] buffer = new byte[size];
            int read;
            if (!ReadProcessMemory(process, new IntPtr(address), buffer, size, out read) || read != size)
                throw new System.ComponentModel.Win32Exception();
            return buffer;
        } finally { CloseHandle(process); }
    }
}
'@

$process = Get-Process -Name Driver -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $process) { throw 'Driver.exe is not running.' }

$types = [DsfMemory]::Read($process.Id, $typesVa, $count * 4)
$values = [DsfMemory]::Read($process.Id, $valuesVa, $count * 4)
$rows = for ($id = 0; $id -lt $count; ++$id) {
    $type = [BitConverter]::ToInt32($types, $id * 4)
    $value = switch ($type) {
        0 { [BitConverter]::ToSingle($values, $id * 4).ToString('R', [Globalization.CultureInfo]::InvariantCulture) }
        1 { if ([BitConverter]::ToInt32($values, $id * 4)) { 'true' } else { 'false' } }
        2 { [BitConverter]::ToInt32($values, $id * 4) }
        default { '' }
    }
    $typeName = switch ($type) { 0 { 'float' } 1 { 'bool' } 2 { 'int' } default { 'unused' } }
    "$id`t$typeName`t$value`t0x{0:X8}" -f [BitConverter]::ToUInt32($values, $id * 4)
}
Set-Content -LiteralPath $Out -Encoding UTF8 -Value (@("id`ttype`tvalue`traw") + $rows)
$used = @($rows | Where-Object { $_ -notmatch "`tunused`t" }).Count
"wrote $Out ($used used parameters of $count)"
