@echo off
cd /d "%~dp0"
echo ============================================
echo  Denia 后端 (api_v2)  :9880  [CPU 常驻]
echo  配置: tts_infer_denia.yaml (denia 中文)
echo  首次加载较慢,看到端口监听后再启动桥
echo  关闭本窗口即停止 Denia
echo ============================================
echo.
.\runtime\python.exe denia_launch.py -c GPT_SoVITS/configs/tts_infer_denia.yaml -a 127.0.0.1 -p 9880
pause
