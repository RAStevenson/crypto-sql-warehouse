@echo off
rem Daily incremental load, launched by Windows Task Scheduler (task name:
rem crypto-warehouse-ingest). The working directory is the repo root, derived
rem from this file's own location, so the task definition holds only one path
rem and the repo can move without editing the schedule.
rem
rem Output appends to logs\scheduled_ingest.log; the log lines carry their own
rem UTC timestamps, and raw.ingest_runs in the database is the real audit
rem trail. The loader resumes from its watermark, so a day the machine slept
rem through is made up in full by the next run.
cd /d "%~dp0.."
if not exist logs mkdir logs
python scripts\run_ingest.py >> logs\scheduled_ingest.log 2>&1
