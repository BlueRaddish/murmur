# Build murmur.exe (dist\murmur\) and, if Inno Setup is installed, murmur-setup.exe (dist\).
$ErrorActionPreference = "Continue"  # native exes write progress to stderr; PS 5.1 would treat that as failure
Set-Location $PSScriptRoot
Stop-Process -Name murmur -Force -ErrorAction SilentlyContinue  # a running copy locks dist
python -c "import murmur; murmur.make_icon('idle').save('murmur.ico', sizes=[(16,16),(32,32),(48,48),(64,64)])"
python -m PyInstaller --noconfirm --clean --noconsole --onedir --name murmur --icon murmur.ico `
  --add-data "vocab.txt;." `
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all av `
  --collect-all tokenizers --collect-all huggingface_hub --collect-all pystray `
  --hidden-import pystray._win32 `
  --exclude-module scipy --exclude-module matplotlib --exclude-module torch --exclude-module tensorflow `
  murmur.py
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) { & $iscc /Q installer.iss; if ($LASTEXITCODE -ne 0) { throw "iscc failed" }; Write-Host "built dist\murmur-setup.exe" }
else { Write-Host "Inno Setup not found; dist\murmur\murmur.exe is portable. winget install JRSoftware.InnoSetup for the installer." }
