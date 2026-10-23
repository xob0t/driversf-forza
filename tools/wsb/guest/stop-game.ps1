# Ends the running game inside the sandbox (run-test.ps1 then reports "Driver exited"
# and still collects Driver.log). Used to abort a long run; the sandbox stays up.
Get-Process Driver -ErrorAction SilentlyContinue | Stop-Process -Force
"stopped"
