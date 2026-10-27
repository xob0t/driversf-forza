@echo off
rem Builds the Forza cars from your disc images (assets\*.iso) and installs them into
rem Driver: San Francisco. See README.md.
rem   install.cmd                 build and install every car
rem   install.cmd --uninstall     put the game back as it was
rem   install.cmd --status        what is installed
rem   install.cmd --report        a zip to attach to a bug report
rem   install.cmd --help          every option
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\release\launcher.ps1" %*
set DSFF_EXIT=%ERRORLEVEL%
rem Opened by a double click (no arguments, its own window: cmd /c ""<folder>\install.cmd" "): keep the
rem window open to read the summary. The command line is only read through delayed expansion: a folder
rem name with & or ) in it would break a plain %cmdcmdline%.
if not "%~1"=="" exit /b %DSFF_EXIT%
setlocal EnableDelayedExpansion
set "DSFF_LINE=!cmdcmdline!"
if /i not "!DSFF_LINE:%~nx0=!"=="!DSFF_LINE!" pause
exit /b %DSFF_EXIT%
