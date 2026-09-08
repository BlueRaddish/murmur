# Build murmur.exe (dist\murmur\) and, if Inno Setup is installed, murmur-setup.exe (dist\).
# Size discipline (299 MB -> see NOTES.md): every --exclude-module below is a package that is only
# reached through a lazy or try/except import (pandas/lxml/bs4/xlsxwriter/dateutil via tqdm.pandas()
# and fsspec; pytest/pygments via pandas.conftest and httpx's optional CLI; pydantic via
# huggingface_hub's webhooks; hf_xet via is_xet_available(); tzdata via zoneinfo; sqlite3 via
# filelock's optional ReadWriteLock; PIL._avif/_imagingft are try/except codecs murmur never draws
# with). PyAV (66 MB of FFmpeg) is import-bound only - stubs/rthook_av.py stands in for it.
# huggingface_hub is collected without its .py sources (collect-all would copy 2.9 MB of source
# that duplicates the bytecode already in the archive).
$ErrorActionPreference = "Continue"  # native exes write progress to stderr; PS 5.1 would treat that as failure
Set-Location $PSScriptRoot
Stop-Process -Name murmur -Force -ErrorAction SilentlyContinue  # a running copy locks dist
python brand.py --ico murmur.ico
python -m PyInstaller --noconfirm --clean --noconsole --onedir --name murmur --icon murmur.ico `
  --add-data "vocab.txt;." --add-data "promptify.txt;." --add-data "murmur.ico;." `
  --collect-all faster_whisper --collect-all ctranslate2 `
  --collect-all tokenizers --collect-all pystray `
  --collect-submodules huggingface_hub --collect-data huggingface_hub --copy-metadata huggingface_hub `
  --hidden-import pystray._win32 `
  --runtime-hook stubs/rthook_av.py --exclude-module av `
  --exclude-module scipy --exclude-module matplotlib --exclude-module torch --exclude-module tensorflow `
  --exclude-module IPython --exclude-module jedi --exclude-module tornado --exclude-module zmq `
  --exclude-module nbformat --exclude-module jsonschema `
  --exclude-module pandas --exclude-module lxml --exclude-module bs4 --exclude-module soupsieve `
  --exclude-module xlsxwriter --exclude-module dateutil `
  --exclude-module pytest --exclude-module _pytest --exclude-module pygments `
  --exclude-module pydantic --exclude-module pydantic_core --exclude-module hf_xet `
  --exclude-module tzdata --exclude-module sqlite3 `
  --exclude-module PIL._avif --exclude-module PIL._imagingft `
  murmur.py
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) { & $iscc /Q installer.iss; if ($LASTEXITCODE -ne 0) { throw "iscc failed" }; Write-Host "built dist\murmur-setup.exe" }
else { Write-Host "Inno Setup not found; dist\murmur\murmur.exe is portable. winget install JRSoftware.InnoSetup for the installer." }
