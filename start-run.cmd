@echo off
chcp 65001 >nul
call "%~dp0scripts\bootstrap.cmd" run %*
pause
