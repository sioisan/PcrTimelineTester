@echo off
rem Run from source without installing: same as the PcrTimelineTester command.
setlocal
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0src"
set "PY=python"
where py >nul 2>nul
if %ERRORLEVEL%==0 set "PY=py -3"
%PY% -m PcrTimelineTester %*
