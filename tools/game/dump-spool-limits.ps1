<#
.SYNOPSIS
Prints how full the vehicle spool budgets are in a running Driver.exe.

.DESCRIPTION
Read-only (PROCESS_VM_READ). Addresses are absolute; Driver.exe 1.04 has a
fixed image base. See research/spool-limits.md for where they come from.

- Graphics pool slots (owner 0x019B3BE0, pool i at +0x0C + i*0x5C:
  {slot array, slot count}; slot = 0x60 bytes, SYS range at +0x00, GPU range
  at +0x2C; range: +0x08 node capacity, +0x14 nodes used, +0x18 byte capacity,
  +0x1C bytes used). Pools 8-11 are Vehicle Global / Spooled / Int / Ext.
- The write-only UID list counter at 0x0188A6B8 (capacity 16384, reset only by
  FUN_004A20C0).
- The shared spool read buffer: owner 0x016DA7D0, heap buffer 0x016DA7D4 and
  its size 0x016DA7D8 (non-zero only while an entry over 0x280000 is loading).

.EXAMPLE
.\tools\game\dump-spool-limits.ps1            # once
.\tools\game\dump-spool-limits.ps1 -Every 5   # every 5 s until Ctrl+C
#>
param([int]$Every = 0, [int[]]$Pools = @(8, 9, 10, 11))

$ErrorActionPreference = 'Stop'
if (-not ('DsfMemory' -as [type])) {
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
}

$names = @{ 8 = 'Vehicle Global'; 9 = 'Vehicle Spooled'; 10 = 'Vehicle Spooled Int'; 11 = 'Vehicle Spooled Ext' }

function U32([byte[]]$bytes, [int]$offset) { [BitConverter]::ToUInt32($bytes, $offset) }

function Show-Once([int]$processId) {
    $lines = @()
    foreach ($pool in $Pools) {
        $head = [DsfMemory]::Read($processId, 0x019B3BE0 + 0x0C + $pool * 0x5C, 8)
        $slots = U32 $head 0
        $count = U32 $head 4
        if ($slots -eq 0 -or $count -eq 0) { $lines += "pool ${pool}: not initialised"; continue }
        $data = [DsfMemory]::Read($processId, $slots, $count * 0x60)
        $peakBytes = 0; $peakNodes = 0; $capBytes = 0; $capNodes = 0; $busy = 0
        for ($slot = 0; $slot -lt $count; ++$slot) {
            $base = $slot * 0x60
            $usedBytes = U32 $data ($base + 0x1C)
            $usedNodes = U32 $data ($base + 0x14)
            $capBytes = U32 $data ($base + 0x18)
            $capNodes = U32 $data ($base + 0x08)
            if ($usedBytes -gt 0) { ++$busy }
            if ($usedBytes -gt $peakBytes) { $peakBytes = $usedBytes }
            if ($usedNodes -gt $peakNodes) { $peakNodes = $usedNodes }
        }
        $lines += ('pool {0,2} {1,-20} slots {2,2} busy {3,2}  SYS bytes max {4,8:N0} / {5,8:N0}  nodes max {6,5} / {7,5}' -f
            $pool, $names[$pool], $count, $busy, $peakBytes, $capBytes, $peakNodes, $capNodes)
    }
    $uid = U32 ([DsfMemory]::Read($processId, 0x0188A6B8, 4)) 0
    $spool = [DsfMemory]::Read($processId, 0x016DA7D0, 12)
    $lines += ('UID list {0} / 16384    spool buffer owner 0x{1:X8} heap 0x{2:X8} size 0x{3:X}' -f
        $uid, (U32 $spool 0), (U32 $spool 4), (U32 $spool 8))
    (Get-Date -Format 'HH:mm:ss') + "`n  " + ($lines -join "`n  ")
}

do {
    $process = Get-Process -Name Driver -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $process) { throw 'Driver.exe is not running.' }
    Show-Once $process.Id
    if ($Every -gt 0) { Start-Sleep -Seconds $Every }
} while ($Every -gt 0)
