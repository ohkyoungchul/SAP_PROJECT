<#
  LS증권 조건검색 자동매매 설치/업데이트 스크립트 (Windows PowerShell 5.1 이상)

  사용법 (명령 프롬프트 또는 PowerShell):
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Target D:\다른\폴더

  하는 일
    1. Python 3.10 이상 확인 (없으면 winget 으로 설치할지 물어봄)
    2. GitHub 에서 최신 코드를 받아 설치 폴더에 복사
       - .env(설정), data(기록), .venv 는 건드리지 않음 → 다시 실행하면 업데이트
    3. 설치 폴더에 가상환경(.venv)을 만들고 필요한 패키지 설치
    4. .env 가 없으면 .env.example 을 복사해 만들고 메모장으로 열기
    5. 실행용 배치 파일 생성: run.bat, list_conditions.bat, update.bat
#>
param(
    [string]$Target = "C:\workspace\Stock_Trading",
    [string]$Repo = "ohkyoungchul/SAP_PROJECT",
    [string]$Branch = "claude/quirky-tesla-le47nl",
    [string]$SourceDir = "",   # 지정하면 다운로드 대신 이 폴더(ls_autotrader)에서 복사 (테스트용)
    [switch]$NoNotepad
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest 진행 표시줄이 매우 느려지는 문제 방지
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Info([string]$msg) { Write-Host "    $msg" }
function Fail([string]$msg) { Write-Host ""; Write-Host "[설치 실패] $msg" -ForegroundColor Red; exit 1 }

# 외부 프로그램(python, pip)이 stderr 에 경고만 출력해도 PowerShell 5.1 이 오류로 멈추지 않게 한다.
# 성공 여부는 종료 코드($LASTEXITCODE)로 판단한다.
function Invoke-Native([scriptblock]$Block) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block } finally { $ErrorActionPreference = $old }
}

# ------------------------------------------------------------------ Python
function Get-PythonCommand {
    $candidates = @(@("py", "-3"), @("python"), @("python3"))
    foreach ($cand in $candidates) {
        $exe = $cand[0]
        $pre = @()
        if ($cand.Count -gt 1) { $pre = $cand[1..($cand.Count - 1)] }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }   # Microsoft Store 바로가기(가짜 python) 제외
        $parts = "$ver".Trim().Split(".")
        if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) {
            return , (@($exe) + $pre)
        }
        Info "Python $ver 발견 (3.10 이상 필요) - 건너뜀"
    }
    return $null
}

function Update-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

Step "Python 확인"
$py = Get-PythonCommand
if (-not $py) {
    Write-Host "    Python 3.10 이상이 설치되어 있지 않습니다." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host "    winget 으로 Python 3.12 를 설치할까요? (Y/N)"
        if ($answer -match '^[Yy]') {
            Invoke-Native { & winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements }
            Update-PathFromRegistry
            $py = Get-PythonCommand
        }
    }
    if (-not $py) {
        Fail ("Python 3.10 이상을 설치한 뒤 이 스크립트를 다시 실행하세요.`n" +
              "    https://www.python.org/downloads/ 에서 설치할 때 'Add python.exe to PATH' 를 체크하세요.")
    }
}
$pyExe = $py[0]
$pyPre = @()
if ($py.Count -gt 1) { $pyPre = $py[1..($py.Count - 1)] }
$pyVer = Invoke-Native { & $pyExe @pyPre --version }
Info "$pyVer ($($py -join ' '))"

# ------------------------------------------------------------------ 코드 받기
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("stock_trading_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    if ($SourceDir) {
        Step "코드 복사 원본: $SourceDir"
        $src = (Resolve-Path $SourceDir).Path
    } else {
        Step "최신 코드 다운로드 ($Repo, $Branch)"
        $zipUrl = "https://codeload.github.com/$Repo/zip/refs/heads/$Branch"
        $zip = Join-Path $tmp "src.zip"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zip
        } catch {
            Fail "다운로드 실패: $zipUrl`n    $($_.Exception.Message)`n    인터넷 연결이나 회사 방화벽을 확인하세요."
        }
        $extract = Join-Path $tmp "x"
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        $src = Get-ChildItem -Path $extract -Directory |
            ForEach-Object { Join-Path $_.FullName "ls_autotrader" } |
            Where-Object { Test-Path (Join-Path $_ "autotrader") } |
            Select-Object -First 1
        if (-not $src) { Fail "받은 파일에서 ls_autotrader 폴더를 찾지 못했습니다." }
    }
    if (-not (Test-Path (Join-Path $src "autotrader\__init__.py")) -and
        -not (Test-Path (Join-Path $src "autotrader/__init__.py"))) {
        Fail "원본 폴더에 autotrader 패키지가 없습니다: $src"
    }

    Step "설치 폴더에 복사: $Target"
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $skipTop = @(".env", "data", ".venv")         # 사용자 설정/기록/가상환경은 보존
    $skipAny = @("__pycache__", ".pytest_cache")
    $srcRoot = (Resolve-Path $src).Path.TrimEnd('\', '/')
    $count = 0
    Get-ChildItem -Path $srcRoot -Recurse -Force -File | ForEach-Object {
        $rel = $_.FullName.Substring($srcRoot.Length).TrimStart('\', '/')
        $segments = $rel -split '[\\/]'
        if ($skipTop -contains $segments[0]) { return }
        foreach ($seg in $segments) { if ($skipAny -contains $seg) { return } }
        $dest = Join-Path $Target $rel
        $destDir = Split-Path -Parent $dest
        if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
        $count++
    }
    Info "파일 $count 개 복사"
} finally {
    Remove-Item -Recurse -Force -Path $tmp -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------ 가상환경 + 패키지
Step "가상환경(.venv) 준비"
$venv = Join-Path $Target ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { $alt = Join-Path $venv "bin/python"; if (Test-Path $alt) { $venvPy = $alt } }
if (-not (Test-Path $venvPy)) {
    Invoke-Native { & $pyExe @pyPre -m venv $venv }
    if ($LASTEXITCODE -ne 0) { Fail "가상환경 생성 실패" }
    $venvPy = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $venvPy)) { $venvPy = Join-Path $venv "bin/python" }
    Info "생성: $venv"
} else {
    Info "기존 가상환경 사용: $venv"
}

Step "필요한 패키지 설치 (requests, websocket-client)"
Invoke-Native { & $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Target "requirements.txt") }
if ($LASTEXITCODE -ne 0) { Fail "패키지 설치 실패 (인터넷 연결 확인)" }

Step "설치 확인"
Push-Location $Target
try {
    Invoke-Native { & $venvPy -c "import autotrader.main, requests, websocket; print('    OK: autotrader 모듈 import 성공')" }
    if ($LASTEXITCODE -ne 0) { Fail "프로그램을 불러오지 못했습니다." }
} finally { Pop-Location }

# ------------------------------------------------------------------ 설정 파일 + 실행 파일
Step "설정 파일(.env)"
$envFile = Join-Path $Target ".env"
$newEnv = $false
if (-not (Test-Path $envFile)) {
    Copy-Item -LiteralPath (Join-Path $Target ".env.example") -Destination $envFile
    $newEnv = $true
    Info ".env 를 새로 만들었습니다. App Key 등을 채워야 합니다."
} else {
    Info "기존 .env 를 그대로 사용합니다."
}

Step "실행용 배치 파일 생성"
$crlf = "`r`n"
$runBat = "@echo off" + $crlf + "cd /d ""%~dp0""" + $crlf +
    """.venv\Scripts\python.exe"" -m autotrader %*" + $crlf + "pause" + $crlf
$listBat = "@echo off" + $crlf + "cd /d ""%~dp0""" + $crlf +
    """.venv\Scripts\python.exe"" -m autotrader --list-conditions" + $crlf + "pause" + $crlf
$updateBat = "@echo off" + $crlf + "cd /d ""%~dp0""" + $crlf +
    "powershell -NoProfile -ExecutionPolicy Bypass -File ""%~dp0install.ps1"" -Target ""%~dp0."" -NoNotepad" + $crlf +
    "pause" + $crlf
[IO.File]::WriteAllText((Join-Path $Target "run.bat"), $runBat, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $Target "list_conditions.bat"), $listBat, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $Target "update.bat"), $updateBat, [Text.Encoding]::ASCII)
Info "run.bat              : 자동매매 실행"
Info "list_conditions.bat  : 서버에 저장된 조건식 목록 확인"
Info "update.bat           : 최신 코드로 업데이트 (.env, data 보존)"

Write-Host ""
Write-Host "설치 완료: $Target" -ForegroundColor Green
Write-Host "다음 순서로 진행하세요:"
Write-Host "  1) .env 에 LS_APPKEY, LS_APPSECRET, LS_USER_ID, CONDITION_NAME 입력 후 저장 (UTF-8)"
Write-Host "  2) list_conditions.bat 실행 → 조건식 이름이 보이는지 확인"
Write-Host "  3) 장중에 run.bat 실행 (처음엔 DRY_RUN=true 로 주문 없이 확인)"
if ($newEnv -and -not $NoNotepad) {
    Start-Process notepad.exe -ArgumentList "`"$envFile`""
}
