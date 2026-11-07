@echo off
rem The car manager window: the installed cars (enable, disable), who drives in traffic, the
rem traffic sets and interesting vehicles. Same launcher as install.cmd (uv, the game folder).
rem   cars.cmd              the window
rem   cars.cmd --help       the car manager's commands (dsff car_manager)
rem setlocal: DSFF_LAUNCH stays in this script, so an install.cmd run later in the same
rem window still installs
setlocal
set DSFF_LAUNCH=car_manager
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\release\launcher.ps1" %*
set DSFF_EXIT=%ERRORLEVEL%
if not "%~1"=="" exit /b %DSFF_EXIT%
setlocal EnableDelayedExpansion
set "DSFF_LINE=!cmdcmdline!"
if /i not "!DSFF_LINE:%~nx0=!"=="!DSFF_LINE!" if not "%DSFF_EXIT%"=="0" pause
exit /b %DSFF_EXIT%
