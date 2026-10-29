[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$OutputDirectory = "dist",
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = "Stop"
if (-not [Environment]::Is64BitProcess) { throw "Build the win-x64 patcher with a 64-bit Python interpreter." }
if ($Version -notmatch '^\d+(\.\d+){0,3}$') { throw "Version must contain one to four numeric dot-separated parts." }
$pythonMajorMinor = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($pythonMajorMinor -ne "3.12") { throw "Build the patcher with Python 3.12 (found $pythonMajorMinor)." }
$pythonBits = (& $Python -c "import struct; print(struct.calcsize('P') * 8)").Trim()
if ($pythonBits -ne "64") { throw "Build the win-x64 patcher with a 64-bit Python interpreter (found $pythonBits-bit)." }
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$requirements = Join-Path $project "requirements-build.txt"
$versionInfo = Join-Path $project "version_info.txt"
$icon = Join-Path $project "..\..\src\frontend\public\favicon.ico"
$work = Join-Path $project "build"
$spec = Join-Path $project "spec"
$output = Join-Path $project $OutputDirectory
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw "Patcher icon is missing: $icon" }
if (-not (Test-Path -LiteralPath $versionInfo -PathType Leaf)) { throw "Patcher version metadata is missing: $versionInfo" }
$versionParts = @($Version.Split('.') | ForEach-Object { [int]$_ })
while ($versionParts.Count -lt 4) { $versionParts += 0 }
$versionTuple = $versionParts -join ', '
New-Item -ItemType Directory -Force -Path $work | Out-Null
New-Item -ItemType Directory -Force -Path $output | Out-Null
# The release workflow uploads dist/*. Leaving a checksum/SBOM from an older
# build here would publish an artefact that does not have a matching EXE.
Get-ChildItem -LiteralPath $output -File |
    Where-Object { $_.Name -match '^BookFlowKoboPatcher.*\.exe(\.sha256|\.spdx\.json)?$' } |
    Remove-Item -Force
$generatedVersionInfo = Join-Path $work "version_info.generated.txt"
$template = Get-Content -Raw -Path $versionInfo
$template = $template.Replace('{{VERSION_TUPLE}}', $versionTuple).Replace('{{VERSION}}', $Version)
Set-Content -NoNewline -Encoding ascii -Path $generatedVersionInfo -Value $template

& $Python -m pip install --requirement $requirements
if ($LASTEXITCODE -ne 0) { throw "Installing the build dependencies failed." }
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
    --name "BookFlowKoboPatcher" --icon $icon --version-file $generatedVersionInfo `
    --paths (Split-Path -Parent $project) `
    --distpath $output --workpath $work --specpath $spec `
    (Join-Path $project "entry.py")
if ($LASTEXITCODE -ne 0) { throw "Building the patcher failed." }

$exe = Join-Path $output "BookFlowKoboPatcher.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller did not produce $exe" }
$versionedName = "BookFlowKoboPatcher-$Version-win-x64.exe"
$versionedExe = Join-Path $output $versionedName
Move-Item -Force -LiteralPath $exe -Destination $versionedExe
$hash = (Get-FileHash -Algorithm SHA256 $versionedExe).Hash.ToLowerInvariant()
Set-Content -NoNewline -Encoding ascii -Path "$versionedExe.sha256" -Value "$hash  $versionedName"
$pythonVersion = (& $Python -c "import platform; print(platform.python_version())").Trim()

$sbom = @{
    spdxVersion = "SPDX-2.3"
    dataLicense = "CC0-1.0"
    SPDXID = "SPDXRef-DOCUMENT"
    name = "BookFlowKoboPatcher"
    bookflow_kobo_patcher = @{ protocol_version = 1; target = "win-x64" }
    documentNamespace = "https://bookflow.local/sbom/kobo-patcher/$Version"
    creationInfo = @{ created = (Get-Date).ToUniversalTime().ToString("o"); creators = @("Tool: PyInstaller") }
    packages = @(
        @{ SPDXID = "SPDXRef-BookFlowKoboPatcher"; name = "BookFlow Kobo Patcher"; versionInfo = $Version; downloadLocation = "NOASSERTION" },
        @{ SPDXID = "SPDXRef-Python"; name = "Python"; versionInfo = $pythonVersion; downloadLocation = "https://www.python.org/" },
        @{ SPDXID = "SPDXRef-PyInstaller"; name = "PyInstaller"; versionInfo = "6.15.0"; downloadLocation = "https://pypi.org/project/pyinstaller/6.15.0/" }
    )
} | ConvertTo-Json -Depth 6
Set-Content -Encoding utf8 -Path "$versionedExe.spdx.json" -Value $sbom
Write-Host "Built $versionedExe"
Write-Host "SHA-256 $hash"
