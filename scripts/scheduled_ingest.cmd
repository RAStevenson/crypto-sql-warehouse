@echo off
rem Daily incremental load, launched by Windows Task Scheduler (task name:
rem crypto-warehouse-ingest). The working directory is the repo root, derived
rem from this file's own location, so the task definition holds only one path
rem and the repo can move without editing the schedule.
rem
rem Three steps, each logged: the load (which refreshes the marts through
rem dbt), the warehouse checks (every dbt test and the timezone check), and
rem dbt's own freshness check on core.ohlcv, which says how old the newest
rem candle is. The checks run even when the load failed, so the log says
rem what state the warehouse was left in.
rem
rem Output appends to logs\scheduled_ingest.log; the log lines carry their own
rem UTC timestamps, and raw.ingest_runs in the database is the real audit
rem trail. The loader resumes from its watermark, so a day the machine slept
rem through is made up in full by the next run.
rem
rem Task Scheduler starts this with no activated environment, so the .venv
rem the README has the reader create is used when it exists; otherwise the
rem python on PATH.
cd /d "%~dp0.."
if not exist logs mkdir logs
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
"%PYTHON%" scripts\run_ingest.py >> logs\scheduled_ingest.log 2>&1
"%PYTHON%" scripts\run_verify.py >> logs\scheduled_ingest.log 2>&1
"%PYTHON%" scripts\run_dbt.py source freshness >> logs\scheduled_ingest.log 2>&1
