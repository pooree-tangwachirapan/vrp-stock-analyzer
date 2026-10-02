' Starts the VRP helper with no console window and leaves it running.
' Nothing here depends on Claude: this is python running vrp.py from this folder.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
sh.Run "pythonw.exe """ & here & "\vrp.py"" --no-browser", 0, False
WScript.Sleep 1500
sh.Run "http://localhost:8765/index.html", 1, False
