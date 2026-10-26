@echo off
rem Khinsider Sync for Windows - double-click to start, then use the page that opens in your browser.
rem Options go after the name, e.g.:  start.bat --data D:\path\to\music (start.bat --help lists them all)
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
  echo First start: setting things up, this takes a minute...
  %PY% -m venv .venv || goto :nopython
)
".venv\Scripts\python.exe" -c "import khinsider_sync, fastapi, uvicorn, httpx, selectolax, curl_cffi, mutagen, PIL" >nul 2>nul
if errorlevel 1 (
  echo Installing what Khinsider Sync needs...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e . || goto :failed
)

".venv\Scripts\python.exe" -m khinsider_sync %*
if errorlevel 1 pause
goto :eof

:nopython
echo.
echo Python 3.9 or newer is needed: https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during installation), then run start.bat again.
pause
goto :eof

:failed
echo.
echo Installing failed - see the messages above. Check your internet connection and try again.
pause
