@echo off
rem FTA 一键健康检查 (只读) — 输出 logs\health_report_YYYY-MM-DD.md
rem 用 --skip-tests 可跳过回归测试(更快)
cd /d "%~dp0"

python tools\health_check.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] 健康检查失败, 详见上方输出
)

echo.
pause
