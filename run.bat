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

REM ---- live app ----
REM This used to build and open dashboard.html, a static snapshot that
REM re-implemented TOPSIS in JavaScript. That meant two implementations of one
REM algorithm which could disagree, and the JavaScript one was what anybody
REM watching actually looked at. The server below serves the real pipeline, so
REM every number on screen is the number the Python produced.
echo [7/7] Starting the live app...
echo.
echo   Opening http://127.0.0.1:8000 in your browser shortly.
echo   Leave this window open -- it IS the server.
echo   Press Ctrl+C here to stop it.
echo.
echo ================================================================

REM Open the browser a few seconds from now, in the background, so the server
REM has time to bind the port. Without the delay the browser beats it and
REM shows a connection error on the first load.
REM
REM ping, not timeout, for the delay: timeout aborts with "input redirection
REM is not supported" whenever stdin is not a real console, which is exactly
REM how this runs when launched by double-click from some shells.
REM No nested quotes either -- an inner start "" would close the cmd /c string
REM early and the rest would be parsed as separate arguments.
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start http://127.0.0.1:8000"

REM Foreground on purpose: this window stays alive as the server, and closing
REM it stops the server. Backgrounding it leaves an orphan holding port 8000,
REM and the next run fails with an address-already-in-use error that looks
REM nothing like its cause.
python -m scripts.serve

echo.
echo Server stopped.
pause >nul
