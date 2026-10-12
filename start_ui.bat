@echo off
cd /d "%~dp0"
echo ============================================
echo   FTA Foreign Trade Assistant
echo   Starting Streamlit server...
echo ============================================
echo.
echo   Panel: http://localhost:8501
echo   Press Ctrl+C to stop the server
echo.
python -m streamlit run ui_review.py --server.port 8501
pause
