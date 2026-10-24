@echo off
cd /d %~dp0
python pokeca_notifier.py >> run.log 2>&1
