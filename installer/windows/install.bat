@echo off
setlocal
chcp 65001 >nul
title MCP for Blender Lite - Kurulum / Setup

rem Everything this installer needs sits next to it: the server wheel, the
rem Blender addon zip and setup_clients.py. Only pip talks to the internet,
rem once, to fetch the "mcp" library.

set "HERE=%~dp0"
if "%MCPBL_HOME%"=="" set "MCPBL_HOME=%LOCALAPPDATA%\mcp-for-blender-lite"

echo.
echo  MCP for Blender Lite
echo  ====================
echo.

rem ---- 1. Python ---------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if "%PY%"=="" (where python >nul 2>nul && set "PY=python")
if "%PY%"=="" (
  echo  Python bulunamadi / Python not found.
  echo  https://www.python.org/downloads/  ^(3.10+, "Add python.exe to PATH" isaretli^)
  echo.
  pause
  exit /b 1
)

rem ---- 2. Server into its own folder ---------------------------------------
echo  [1/3] Sunucu kuruluyor / Installing the server...
if not exist "%MCPBL_HOME%\venv\Scripts\python.exe" (
  %PY% -m venv "%MCPBL_HOME%\venv" || goto :fail
)
set "VPY=%MCPBL_HOME%\venv\Scripts\python.exe"
for %%W in ("%HERE%mcp_for_blender_lite-*.whl") do set "WHEEL=%%~fW"
if "%WHEEL%"=="" (
  echo  .whl dosyasi bu klasorde yok / wheel file missing next to install.bat
  goto :fail
)
"%VPY%" -m pip install --quiet --disable-pip-version-check --upgrade "%WHEEL%" || goto :fail

rem ---- 3. Blender addon + MCP clients ---------------------------------------
echo  [2/3] Blender eklentisi / Blender addon...
echo  [3/3] Claude baglantisi / Claude connection...
"%VPY%" "%HERE%setup_clients.py" "%MCPBL_HOME%\venv\Scripts\mcp-for-blender-lite.exe" || goto :fail

echo.
echo  Tamam / Done.
echo  Blender'da N tusu ^> MCP Lite ^> Connect. Claude'u yeniden baslatin.
echo  In Blender press N ^> MCP Lite ^> Connect. Restart Claude.
echo.
pause
exit /b 0

:fail
echo.
echo  Kurulum tamamlanamadi / Setup failed. Yukaridaki mesaja bakin / see the message above.
echo.
pause
exit /b 1
