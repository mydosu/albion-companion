Set sh = CreateObject("WScript.Shell")
sh.Run """" & WScript.Arguments(0) & """ -hidden", 0, False
