' Hermes gateway watchdog - window-less launcher (ASCII only on purpose)
' Called by the Windows scheduled task "Hermes Gateway Watchdog" every 5 minutes via wscript.exe.
' WScript.Shell.Run(cmd, 0, True): 0 = hidden window, True = wait for completion.
' Do not use start /min, bare cmd, or a hand-written .bat.
Option Explicit

Dim fso, sh, ps1, cmd
Set fso = CreateObject("Scripting.FileSystemObject")

ps1 = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "hermes_watchdog.ps1")
If Not fso.FileExists(ps1) Then WScript.Quit 2

Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & ps1 & """"
sh.Run cmd, 0, True

WScript.Quit 0
