# Builds dist\forever-fishing.exe: one file, no console window, Python and all libraries inside.
Set-Location $PSScriptRoot
python -m PyInstaller --noconfirm --clean --onefile --windowed --name forever-fishing fishbot_gui.pyw
