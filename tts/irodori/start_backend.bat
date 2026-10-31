@echo off
rem ASCII 转发壳（2026-09-19）：静默启动层 _静默运行.ps1 -> cmd 传中文文件名会坏掉，
rem 所以静默层改调这个英文名；中文名由 cmd 自己解析（bat 内容无编码问题）。
rem 旧的中文名 bat 原样保留，双击照旧可用。
rem 2026-10-30: keep the ja text of each rendered block in the log (see render_queue.speech_log)
if not defined TTS_LOG_TEXT set "TTS_LOG_TEXT=1"
call "%~dp0启动Irodori后端.bat"
