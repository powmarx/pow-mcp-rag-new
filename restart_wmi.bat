@echo off
REM Restart Windows Management Instrumentation (WMI) service to unblock SQLite/ChromaDB
REM This fixes a known issue where WMI locks file I/O and causes database hangs.
REM Requires Administrator privileges.

net stop winmgmt /y
net start winmgmt
