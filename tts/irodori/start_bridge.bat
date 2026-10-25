@echo off
rem ASCII 转发壳（2026-09-19）：静默启动层 _静默运行.ps1 -> cmd 传中文文件名会坏掉，
rem 所以静默层改调这个英文名；中文名由 cmd 自己解析（bat 内容无编码问题）。
rem 旧的中文名 bat 原样保留，双击照旧可用。
call "%~dp0启动TTS桥.bat"
