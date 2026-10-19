@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "RENDERER=manim_multi_question_openai_mux_fixed.py"
if not exist "%RENDERER%" (echo ERROR: Renderer not found & exit /b 1)
if not defined OPENAI_API_KEY (echo ERROR: Set OPENAI_API_KEY before rendering & exit /b 1)
if not exist "videos" mkdir "videos"
set "PART=%~1"
if not defined PART (echo Usage: render_one_part.bat 2 & exit /b 2)
if %PART% LSS 1 (echo Invalid part & exit /b 2)
if %PART% GTR 13 (echo Invalid part & exit /b 2)
if %PART% LSS 10 (set "PART=0%PART%")
set "YAML_FILE=part_%PART%_animations.yaml"
if not exist "%YAML_FILE%" (echo Missing %YAML_FILE% & exit /b 1)
set "MANIM_YAML_FILE=%CD%\%YAML_FILE%"
echo Rendering part %PART%...
manim -qh --media_dir "videos\part_%PART%" -o "part_%PART%.mp4" "%RENDERER%" YAMLAnimation
if errorlevel 1 (echo Rendering failed for part %PART% & exit /b 1)
echo Finished part %PART%.
endlocal
