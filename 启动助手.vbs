Option Explicit
Dim fs, shell, root, pythonw, candidates, candidate
Set fs = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fs.GetParentFolderName(WScript.ScriptFullName)
candidates = Array(root & "\.venv\Scripts\pythonw.exe", shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe")
pythonw = ""
For Each candidate In candidates
    If fs.FileExists(candidate) Then
        pythonw = candidate
        Exit For
    End If
Next
If pythonw = "" Then
    MsgBox "Python was not found. Please see code/README.md for setup, then run app.py.", 48, "SA Assistant"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & root & "\app.py" & Chr(34), 0, False
