@echo off
chcp 65001 >nul
call "%~dp0scripts\bootstrap.cmd" view %*
pause
