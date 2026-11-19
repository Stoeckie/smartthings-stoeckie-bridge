set rc=%errorlevel%
if %rc%==75 goto loop
if exist update-pending.flag if exist stoeckie_bridge.prev.py (copy /y stoeckie_bridge.prev.py stoeckie_bridge.py >nul & del /q update-pending.flag & goto loop)
if not %rc%==0 (echo %date% %time% bridge stopped with code %rc%, start again in 30 s>> run-restarts.log & ping -n 31 127.0.0.1 >nul & goto loop)
exit /b %rc%
