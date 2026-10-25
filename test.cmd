@echo off
python -m pytest %*
exit /b %ERRORLEVEL%
