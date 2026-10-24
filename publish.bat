@echo off
rem Khinsider Sync - send all changes to GitHub, and publish a GitHub release when the version changed.
rem   Double-click it, or:  publish.bat "what changed"
rem A release is made for the version in pyproject.toml (e.g. 1.2.0 -> release v1.2.0) the first
rem time that version is pushed. Then click "Compose Up" on Unraid to update the container.
setlocal
cd /d "%~dp0"

where git >nul 2>nul || (echo Git is not installed. Run:  winget install --id Git.Git -e& goto :fail)
where gh  >nul 2>nul || (echo GitHub CLI is not installed. Run:  winget install --id GitHub.cli -e& goto :fail)

set "MSG=%~1"
if "%MSG%"=="" set "MSG=Update %DATE% %TIME:~0,5%"
set "VER="
for /f "tokens=2 delims== " %%v in ('findstr /b /c:"version = " pyproject.toml') do set "VER=%%~v"
if "%VER%"=="" (echo Could not read the version from pyproject.toml.& goto :fail)

echo Getting anything new from GitHub first...
git pull --rebase --autostash -q || goto :fail

git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -q -m "%MSG%" || goto :fail
  echo Saved your changes: %MSG%
) else (
  echo No new changes to save.
)

echo Sending to GitHub...
git push -q || goto :fail

gh release view "v%VER%" >nul 2>nul
if errorlevel 1 (
  gh release create "v%VER%" --title "Khinsider Sync %VER%" --generate-notes --target main || goto :fail
  echo Published release v%VER%.
) else (
  echo Release v%VER% already exists - a new release is made when the version in pyproject.toml changes.
)

echo.
echo Done. On Unraid: Docker ^> Compose ^> khinsider-sync ^> Compose Up.
if not defined NOPAUSE pause
exit /b 0

:fail
echo.
echo Something went wrong - see the message above.
if not defined NOPAUSE pause
exit /b 1
