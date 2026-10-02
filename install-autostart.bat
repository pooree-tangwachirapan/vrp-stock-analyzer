@echo off
rem Puts a shortcut in your Startup folder so the helper is up every time you
rem log in. Nothing to do with Claude. Delete the shortcut to undo it.
set "TARGET=%~dp0VRP-silent.vbs"
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VRP Analyzer.lnk"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
  "$s.TargetPath='wscript.exe';" ^
  "$s.Arguments='\"%TARGET%\"';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.Description='VRP Stock Analyzer helper';" ^
  "$s.Save()"
echo.
echo Installed. The helper will start automatically next time you log in.
echo To undo, delete:
echo   %LINK%
echo.
pause
