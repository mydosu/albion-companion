' launch/hidden.vbs - run something without a console window.
'   * drag a .bat/.exe/.ps1 onto this file -> it is launched hidden (path is quoted for you)
'   * wscript launch/hidden.vbs "some command line" -> the command line is run as-is
' Used by the AlbionBrainWatch scheduled task and by ????.bat's silent layer.
Option Explicit
Dim fso, sh, target
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count = 0 Then WScript.Quit 1
target = WScript.Arguments(0)
If fso.FileExists(target) Then
  sh.Run """" & target & """", 0, False
Else
  sh.Run target, 0, False
End If