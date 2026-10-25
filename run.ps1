# 一键起锅：切到脚本目录，然后跑 up
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
python .\feiyu_bot.py up
