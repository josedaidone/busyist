# Builds the Windows installer and a portable zip into dist\.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Needs Python 3.11+ (the "py" launcher) and Inno Setup 6. Everything else is
# installed into .venv-build. Output:
#   dist\Buzyist-Setup-<version>.exe
#   dist\Busyist-Setup-<version>.exe  (the same installer, for copies from before the rename)
#   dist\Buzyist-<version>-portable.zip
#   dist\SHA256SUMS.txt
#   dist\SHA256SUMS.txt.minisig      (only with -SigningKey, see docs\RELEASING.md)
#
# -SigningKey <minisign .key file> (or $env:BUZYIST_SIGNING_KEY) signs
# SHA256SUMS.txt for the in-app updater; the password comes from
# $env:MINISIGN_PASSWORD or is asked for. CI signs in its own step instead.
param([switch]$SkipInstaller, [string]$SigningKey = $env:BUZYIST_SIGNING_KEY)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

$version = (Select-String -Path busyist.py -Pattern '__version__ = "([\d.]+)"').Matches[0].Groups[1].Value
Write-Host "Building buZyist $version" -ForegroundColor Cyan

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
Run $py @("tools\third_party_licenses.py", "dist\Buzyist\THIRD_PARTY_LICENSES.txt")
Copy-Item LICENSE dist\Buzyist\LICENSE.txt

# Never ship anyone's settings: the app keeps them in %APPDATA%, but check.
$leaks = Get-ChildItem dist\Buzyist -Recurse -File | Where-Object { $_.Name -in @("config.json", "session.json", "history.json", "busybar_todoist.json") }
if ($leaks) { throw "Personal files ended up in the build: $($leaks.FullName -join ', ')" }

# 3. Portable zip.
$zip = "dist\Buzyist-$version-portable.zip"
Compress-Archive -Path dist\Buzyist -DestinationPath $zip -CompressionLevel Optimal

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
    # Copies from before the rename (1.4.x and older) update from the release
    # asset named Busyist-Setup-<version>.exe, so ship the installer under that
    # name too (both are listed in SHA256SUMS.txt).
    Copy-Item "dist\Buzyist-Setup-$version.exe" "dist\Busyist-Setup-$version.exe"
}

# 5. Checksums, so people can check what they downloaded.
Get-ChildItem dist -File | Where-Object { $_.Extension -in ".exe", ".zip" } | ForEach-Object {
    "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  $($_.Name)"
} | Set-Content dist\SHA256SUMS.txt -Encoding ascii

# 6. Sign the checksums (#022): installed copies with UPDATE_PUBKEYS refuse an unsigned update.
if ($SigningKey) {
    Run $py @("tools\minisign.py", "sign", "-s", $SigningKey, "-m", "dist\SHA256SUMS.txt", "--app", "busyist.py")
} else {
    Write-Warning "SHA256SUMS.txt is not signed here (pass -SigningKey; CI signs in its own step)."
}

Write-Host "`nDone:" -ForegroundColor Green
Get-ChildItem dist -File | ForEach-Object { "  {0,-40} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB) }
