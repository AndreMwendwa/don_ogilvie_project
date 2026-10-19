@echo off
setlocal EnableExtensions
cd /d "%~dp0"
for /L %%N in (2,1,13) do (
  call "%~dp0render_one_part.bat" %%N
  if errorlevel 1 (echo Stopped after a failed render. & exit /b 1)
)
echo Parts 2-13 completed successfully.
endlocal
pause
