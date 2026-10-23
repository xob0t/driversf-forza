# Copies crash dumps from the guest run folder to C:\Output\dumps.
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path C:\Output\dumps | Out-Null
Get-ChildItem C:\Run -Filter *.dmp -ErrorAction SilentlyContinue | Copy-Item -Destination C:\Output\dumps -Force
Get-ChildItem C:\Output\dumps | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize |
    Out-String | Set-Content C:\Output\dumps\list.txt
