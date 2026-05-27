@echo off
echo === OPTIONS SCANNER - Railway Deploy ===
cd /d %~dp0
railway login
railway init
railway up
echo.
echo === Deploy abgeschlossen ===
pause
