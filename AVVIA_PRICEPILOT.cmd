@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-pp\Scripts\python.exe" (
  echo Ambiente PricePilot mancante. Consulta docs\AVVIO_LOCALE.md.
  pause
  exit /b 1
)
echo Avvio PricePilot su http://127.0.0.1:8501
echo Per fermare il server premi Ctrl+C.
start "" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8501'"
".venv-pp\Scripts\python.exe" -m streamlit run pricepilot\dashboard\app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
pause
