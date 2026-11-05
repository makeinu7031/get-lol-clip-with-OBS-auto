@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LoL Auto Clip
python -c "import requests, obsws_python" >nul 2>&1
if errorlevel 1 (
  echo 必要なライブラリをインストールします...
  python -m pip install requests obsws-python
)
python lol_obs_autoclip.py
pause
