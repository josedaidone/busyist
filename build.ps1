# Builds the Windows installer and a portable zip into dist\.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Needs Python 3.11+ (the "py" launcher) and Inno Setup 6. Everything else is
# installed into .venv-build. Output:
#   dist\Busyist-Setup-<version>.exe
#   dist\Busyist-<version>-portable.zip
#   dist\SHA256SUMS.txt
param([switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

$version = (Select-String -Path busyist.py -Pattern '__version__ = "([\d.]+)"').Matches[0].Groups[1].Value
Write-Host "Building Busyist $version" -ForegroundColor Cyan

# 1. Build environment, kept apart from the one used to run from source.
$py = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"
if (-not (Test-Path $py)) {
    # BUILD_PYTHON picks the interpreter (CI sets it to "python").
    if ($env:BUILD_PYTHON) { Run $env:BUILD_PYTHON @("-m", "venv", ".venv-build") }
    else { Run "py" @("-3.11", "-m", "venv", ".venv-build") }
}
Run $py @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Run $py @("-m", "pip", "install", "--quiet", "-r", "requirements.txt", "-r", "requirements-build.txt")

# 2. The app folder.
Remove-Item build, dist -Recurse -Force -ErrorAction SilentlyContinue
Run $py @("-m", "PyInstaller", "packaging\busyist.spec", "--noconfirm", "--distpath", "dist", "--workpath", "build\pyinstaller")
Run $py @("tools\third_party_licenses.py", "dist\Busyist\THIRD_PARTY_LICENSES.txt")
Copy-Item LICENSE dist\Busyist\LICENSE.txt

# Never ship anyone's settings: the app keeps them in %APPDATA%, but check.
$leaks = Get-ChildItem dist\Busyist -Recurse -File | Where-Object { $_.Name -in @("config.json", "session.json", "history.json", "busybar_todoist.json") }
if ($leaks) { throw "Personal files ended up in the build: $($leaks.FullName -join ', ')" }

# 3. Portable zip.
$zip = "dist\Busyist-$version-portable.zip"
Compress-Archive -Path dist\Busyist -DestinationPath $zip -CompressionLevel Optimal

# 4. Installer.
if (-not $SkipInstaller) {
    $bootstrapper = "build\MicrosoftEdgeWebview2Setup.exe"
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $bootstrapper -UseBasicParsing
    $sig = Get-AuthenticodeSignature $bootstrapper
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
        throw "The WebView2 bootstrapper is not signed by Microsoft ($($sig.Status))."
    }
    $iscc = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php" }
    Run $iscc @("/Q", "/DAppVersion=$version", "packaging\busyist.iss")
}

# 5. Checksums, so people can check what they downloaded.
Get-ChildItem dist -File | Where-Object { $_.Extension -in ".exe", ".zip" } | ForEach-Object {
    "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  $($_.Name)"
} | Set-Content dist\SHA256SUMS.txt -Encoding ascii

Write-Host "`nDone:" -ForegroundColor Green
Get-ChildItem dist -File | ForEach-Object { "  {0,-40} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB) }
