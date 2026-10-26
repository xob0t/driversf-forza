@echo off
rem Builds the Forza cars from your disc images (assets\*.iso) and installs them into
rem Driver: San Francisco. See README.md.
rem   install.cmd                 build and install every car
rem   install.cmd --uninstall     put the game back as it was
rem   install.cmd --status        what is installed
rem   install.cmd --help          every option
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\release\launcher.ps1" %*
set DSFF_EXIT=%ERRORLEVEL%
rem Opened by a double click (no arguments, its own window): keep the window open to read the summary.
if "%~1"=="" echo %cmdcmdline% | "%SystemRoot%\System32\find.exe" /i "%~0" >nul && pause
exit /b %DSFF_EXIT%
