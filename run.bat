@echo off
cd /d %~dp0
start cmd /k python Danmu.py
start cmd /k python push_AB_windows.py
