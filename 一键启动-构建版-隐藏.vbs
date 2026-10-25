' 阿尔比恩 · 一键启动（隐藏版）——双击它，不弹任何窗口
' 默认跑「构建版」；想把别的 bat 隐藏跑，把那个 bat 拖到这个 vbs 上即可
Option Explicit
Dim fso, sh, bat, log, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
bat = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "一键启动-构建版.bat")
If WScript.Arguments.Count > 0 Then bat = WScript.Arguments(0)
If Not fso.FileExists(bat) Then
  MsgBox "找不到启动脚本：" & vbCrLf & bat, 16, "阿尔比恩"
  WScript.Quit 1
End If
log = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "logs\start_hidden.log")
sh.CurrentDirectory = fso.GetParentFolderName(bat)
' 真正的静默靠 _silent_run.ps1 里的 CreateNoWindow（Windows Terminal 默认宿主下 SW_HIDE 不可靠）
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "_silent_run.ps1") & """ -Bat """ & bat & """ -Log """ & log & """"
sh.Run cmd, 0, False
