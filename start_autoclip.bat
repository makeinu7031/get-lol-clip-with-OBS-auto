@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LoL Auto Clip

REM OBSが起動していなければ起動する（パスは環境に合わせて調整してください）
tasklist /FI "IMAGENAME eq obs64.exe" 2>NUL | find /I /N "obs64.exe">NUL
if "%ERRORLEVEL%"=="1" (
  echo OBSを起動します...
  start "" "C:\Program Files\obs-studio\bin\64bit\obs64.exe"
  timeout /t 5 /nobreak >nul
)

python -c "import requests, obsws_python" >nul 2>&1
if errorlevel 1 (
  echo 必要なライブラリをインストールします...
  python -m pip install requests obsws-python
)

echo スクリプトを起動します...
python get_lol_obs_autoclip.py
pause
