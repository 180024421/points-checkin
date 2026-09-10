@echo off
cd /d %~dp0
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm CheckinTool.spec
echo.
echo Output: dist\CheckinTool.exe
pause
