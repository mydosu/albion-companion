' Start the status widget without a console window.
'
' Interpreter lookup order (2026-11-02: this file used to hard-code the
' author's %LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe, so a fresh
' machine could not start the widget):
'   1. ALBION_PYTHONW, an explicit pythonw.exe path
'   2. pythonw.exe on PATH
'   3. pyw.exe on PATH (the windowed Python launcher)
'   4. py.exe on PATH (window style 0 hides its console)
'   5. the author's original fixed path, if it exists
'
' A missing interpreter is written to %TEMP%\albion_status_widget.err.log and
' exits 1; no modal dialog, so the one-click launcher is never blocked.
Option Explicit

Dim shell, fso, here, target, py, fallback, logPath

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
target = fso.BuildPath(here, "albion_status_widget.pyw")

If Not fso.FileExists(target) Then
    LogFailure "widget script not found: " & target
    WScript.Quit 1
End If

py = ExplicitPython()
If py = "" Then py = OnPath("pythonw.exe")
If py = "" Then py = OnPath("pyw.exe")
If py = "" Then py = OnPath("py.exe")
If py = "" Then
    fallback = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe")
    If fso.FileExists(fallback) Then py = fallback
End If
If py = "" Then
    LogFailure "no Python interpreter found; install Python 3.13 (Add to PATH) or set ALBION_PYTHONW"
    WScript.Quit 1
End If

shell.Run """" & py & """ """ & target & """", 0, False

Function ExplicitPython()
    Dim value
    ExplicitPython = ""
    value = shell.ExpandEnvironmentStrings("%ALBION_PYTHONW%")
    If StrComp(value, "%ALBION_PYTHONW%", vbTextCompare) = 0 Then Exit Function
    value = Trim(value)
    If Len(value) > 0 And fso.FileExists(value) Then ExplicitPython = value
End Function

Function OnPath(exeName)
    Dim raw, dirs, i, candidate
    OnPath = ""
    raw = shell.ExpandEnvironmentStrings("%PATH%")
    dirs = Split(raw, ";")
    For i = 0 To UBound(dirs)
        candidate = Trim(dirs(i))
        If Len(candidate) > 0 Then
            If Left(candidate, 1) = """" And Right(candidate, 1) = """" Then
                candidate = Mid(candidate, 2, Len(candidate) - 2)
            End If
            candidate = shell.ExpandEnvironmentStrings(fso.BuildPath(candidate, exeName))
            If fso.FileExists(candidate) Then
                OnPath = candidate
                Exit Function
            End If
        End If
    Next
End Function

Sub LogFailure(message)
    Dim stream
    On Error Resume Next
    logPath = fso.BuildPath(shell.ExpandEnvironmentStrings("%TEMP%"), "albion_status_widget.err.log")
    Set stream = fso.OpenTextFile(logPath, 8, True)
    stream.WriteLine Now & " " & message
    stream.Close
End Sub
