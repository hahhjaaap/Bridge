@echo off
setlocal
python "%~dp0test_next_mcp.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_conpty.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_work_recovery.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_virtual_pointer_mcp.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_backend_recovery_mcp.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_backend_recovery.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_documents_mcp.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_documents.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_desktop_workflow.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_uia_provider.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_tool_consolidation.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_unified.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_extensions.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_audit_regressions.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_session_lifecycle.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_integration.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_terminal_utf8.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_features.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_windows_durability.py"
if errorlevel 1 exit /b %ERRORLEVEL%
python "%~dp0test_optimization.py"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (echo [PASS] UDB Unified single public MCP tool / 60 operations) else (echo [ERROR] UDB Unified test failed exit=%RC%)
exit /b %RC%

