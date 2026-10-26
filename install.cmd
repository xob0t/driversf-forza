@echo off
rem Builds the Forza cars from your disc images (assets\*.iso) and installs them into
rem Driver: San Francisco. See README.md.
rem   install.cmd                 build and install every car
rem   install.cmd --uninstall     put the game back as it was
rem   install.cmd --status        what is installed
rem   install.cmd --help          every option
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\release\launcher.ps1" %*
set DSFF_EXIT=%ERRORLEVEL%
rem Opened by a double click: keep the window open to read the summary.
echo %cmdcmdline% | find /i "%~0" >nul && pause
exit /b %DSFF_EXIT%
