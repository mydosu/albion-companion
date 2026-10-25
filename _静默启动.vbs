Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count = 0 Then WScript.Quit 1
sh.Run WScript.Arguments(0), 0, False
