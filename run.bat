@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ================================================================
echo   E-Commerce Aggregator  -  setup and run
echo   Folder: %CD%
echo ================================================================
echo.

REM ---- sanity check: are we in the right folder? ----
if not exist "src\pipeline.py" (
  echo [ERROR] This script is not in the project folder.
  echo         Expected to find src\pipeline.py next to run.bat
  echo         If your folder looks like ecom-aggregator\ecom-aggregator,
  echo         move run.bat into the INNER folder.
  echo.
  pause
  exit /b 1
)

REM ---- find python ----
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python is not on your PATH.
  echo         Install Python 3.12 from python.org and tick
  echo         "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%v
echo [1/7] Python !PYVER! found
echo.

REM ---- dependencies ----
REM Core + API. sentence-transformers is skipped: it drags in ~2GB of PyTorch
REM and the TF-IDF fallback runs everything without it.
echo [2/7] Installing dependencies (core + API, this is quick)...
python -m pip install --quiet --disable-pip-version-check numpy scikit-learn requests pytest fastapi uvicorn pydantic httpx
if errorlevel 1 (
  echo [ERROR] Dependency install failed. See the message above.
  pause
  exit /b 1
)
echo       done
echo.

REM ---- use the fast backend so nothing stalls on a model download ----
set EMBEDDER_BACKEND=tfidf

REM ---- tests ----
echo [3/7] Running tests...
python -m pytest tests -q
if errorlevel 1 (
  echo.
  echo [WARN] Some tests failed. Continuing anyway so you can still see the demo.
)
echo.

REM ---- price history ----
REM Without this, price_history is empty and the forecasting work has no input.
REM Deterministic, so re-running produces the identical series.
echo [4/7] Generating 180 days of price history...
python -m scripts.backfill_prices --days 180
echo.

REM ---- terminal demo ----
echo [5/7] Pipeline walkthrough
echo ----------------------------------------------------------------
python -m scripts.demo
echo ----------------------------------------------------------------
echo.

REM ---- evaluation ----
echo [6/7] Evaluation tables
echo ----------------------------------------------------------------
python -m scripts.evaluate_matching
python -m scripts.analyse_normalisation
echo ----------------------------------------------------------------
echo.

REM ---- dashboard ----
echo [7/7] Building dashboard...
python -m scripts.build_dashboard
if exist "dashboard.html" (
  echo.
  echo Opening dashboard.html in your browser...
  start "" "dashboard.html"
) else (
  echo [ERROR] dashboard.html was not created.
)

echo.
echo ================================================================
echo   Done.
echo.
echo   To start the live API instead of the static dashboard:
echo       python -m scripts.serve
echo   then open http://127.0.0.1:8000/docs
echo ================================================================
pause >nul
