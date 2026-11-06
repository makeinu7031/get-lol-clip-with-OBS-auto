@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LoL Auto Clip

REM OBSが起動していなければ起動する
REM デスクトップのショートカットで動くなら「リンク先」と「作業フォルダ」をここに合わせる（locale エラー対策で作業フォルダを bin\64bit にする）
set "OBS_BIN=C:\Program Files\obs-studio\bin\64bit"
tasklist /FI "IMAGENAME eq obs64.exe" 2>NUL | find /I /N "obs64.exe">NUL
if "%ERRORLEVEL%"=="1" (
  echo OBSを起動します...
  if not exist "%OBS_BIN%\obs64.exe" (
    echo エラー: obs64.exe が見つかりません: %OBS_BIN%
    echo エクスプローラで obs64.exe を検索するか、動いているショートカットのプロパティでパスを確認してください。
    pause
    exit /b 1
  )
  cd /d "%OBS_BIN%"
  start "" "%OBS_BIN%\obs64.exe"
  REM WebSocket が待ち受けするまで待つ（短いと ConnectionRefused になりやすい）
  timeout /t 20 /nobreak >nul
  cd /d "%~dp0"
)

python -c "import requests, obsws_python" >nul 2>&1
if errorlevel 1 (
  echo 必要なライブラリをインストールします...
  python -m pip install requests obsws-python
)

echo スクリプトを起動します...
python get_lol_obs_autoclip.py
pause
