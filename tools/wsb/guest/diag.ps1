# Writes a snapshot of guest state to C:\Output\diag.txt.
$ErrorActionPreference = 'Continue'
& {
    "== $(Get-Date -Format o) as $(whoami)"
    '== processes'
    Get-Process | Where-Object { $_.ProcessName -match 'Driver|powershell|WerFault' } |
        Format-Table Id, ProcessName, StartTime, CPU, MainWindowTitle -AutoSize | Out-String -Width 200
    '== C:\Run'
    Get-ChildItem C:\Run -Force -ErrorAction SilentlyContinue | Format-Table Mode, Length, Name, Target -AutoSize | Out-String -Width 200
    '== C:\Run\Install'
    Get-ChildItem C:\Run\Install -Force -ErrorAction SilentlyContinue | Format-Table Mode, Length, Name, Target -AutoSize | Out-String -Width 200
    '== C:\Run\plugins (recursive)'
    Get-ChildItem C:\Run\plugins -Recurse -Force -ErrorAction SilentlyContinue | Format-Table Length, FullName -AutoSize | Out-String -Width 200
    '== C:\Run\Common\UserLuaScripts (recursive)'
    Get-ChildItem C:\Run\Common\UserLuaScripts -Recurse -Force -ErrorAction SilentlyContinue | Format-Table Length, FullName -AutoSize | Out-String -Width 200
    '== Driver.log anywhere under C:\Run or the profile'
    Get-ChildItem C:\Run, ([Environment]::GetFolderPath('MyDocuments')) -Filter Driver.log -Recurse -Force -ErrorAction SilentlyContinue | Format-Table Length, LastWriteTime, FullName -AutoSize | Out-String -Width 200
    '== Driver window'
    Get-Process Driver -ErrorAction SilentlyContinue | ForEach-Object {
        Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public static class DiagWin{[StructLayout(LayoutKind.Sequential)]public struct R{public int L,T,Ri,B;}[DllImport("user32.dll")]public static extern bool GetWindowRect(IntPtr w,out R r);[DllImport("user32.dll")]public static extern bool IsIconic(IntPtr w);}' -ErrorAction SilentlyContinue
        $r = New-Object DiagWin+R; [DiagWin]::GetWindowRect($_.MainWindowHandle, [ref]$r) | Out-Null
        "pid $($_.Id) hwnd $($_.MainWindowHandle) rect $($r.L),$($r.T),$($r.Ri),$($r.B) iconic $([DiagWin]::IsIconic($_.MainWindowHandle)) responding $($_.Responding)"
    }
    '== screen'
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.Screen]::AllScreens | ForEach-Object { "$($_.DeviceName) $($_.Bounds)" }
    '== recent application errors'
    Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Level = 2; StartTime = (Get-Date).AddMinutes(-30) } -MaxEvents 10 -ErrorAction SilentlyContinue |
        Format-List TimeCreated, ProviderName, Message | Out-String -Width 200
} *>&1 | Set-Content C:\Output\diag.txt
