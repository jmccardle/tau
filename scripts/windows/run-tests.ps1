<#
.SYNOPSIS
  Test a cross-compiled tau wheel on a real Windows x64 machine, and write one results file.

.DESCRIPTION
  Run from the unpacked bundle (scripts/windows_bundle.sh makes it):

      powershell -ExecutionPolicy Bypass -File .\run-tests.ps1

  It uses the bundled uv.exe to install a pinned CPython and make a venv in .\venv, installs the
  wheel from .\wheels with the test dependencies (from PyPI), then runs:
    1. a smoke check: import, `tau --version`, `tau -p` with the mock model (argument and piped
       stdin), and `tau serve -d` / `tau -p` attached / `tau serve --stop` in a throwaway TAU_HOME;
    2. the serve tests (tests/serve) and the console tests;
    3. the whole suite (skip it with -Quick).
  Everything goes to results-<computer>-<time>.txt next to this script: bring that file back.
  Nothing is installed outside this folder except uv's Python download (in uv's own cache).

.PARAMETER Quick
  Skip step 3, the whole suite.
#>
param([switch]$Quick)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$results = Join-Path $here "results-$env:COMPUTERNAME-$stamp.txt"
$python = Get-Content (Join-Path $here 'PYTHON_VERSION') -Raw
$python = $python.Trim()
$uv = Join-Path $here 'uv.exe'
$venv = Join-Path $here 'venv'
$py = Join-Path $venv 'Scripts\python.exe'
$src = Join-Path $here 'src'
$summary = New-Object System.Collections.Generic.List[string]

function Say([string]$text) {
    Write-Host $text
    Add-Content -Path $results -Value $text -Encoding UTF8
}

# One command-line argument, quoted as the C runtime reads it back (spaces, quotes, backslashes).
function Quote([string]$arg) {
    if ($arg -ne '' -and $arg -notmatch '[\s"]') { return $arg }
    $quoted = $arg -replace '(\\*)"', '$1$1\"'
    $quoted = $quoted -replace '(\\+)$', '$1$1'
    return '"' + $quoted + '"'
}

# Run a native command; its output (stdout and stderr) goes to the screen and the results file.
# Returns its exit code. A command that cannot start is an error code of -1, said in the file.
function Step([string]$name, [string]$exe, [string[]]$arguments, [string]$stdin = $null) {
    Say ''
    Say "===== $name"
    Say ("> " + $exe + ' ' + ($arguments -join ' '))
    $started = Get-Date
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    try {
        $start = @{
            FilePath = $exe; ArgumentList = (($arguments | ForEach-Object { Quote $_ }) -join ' ')
            WorkingDirectory = (Get-Location).Path; NoNewWindow = $true; Wait = $true
            PassThru = $true; RedirectStandardOutput = $out; RedirectStandardError = $err
        }
        if ($stdin -ne $null -and $stdin -ne '') {
            $in = [System.IO.Path]::GetTempFileName()
            [System.IO.File]::WriteAllText($in, $stdin)
            $start.RedirectStandardInput = $in
        }
        $process = Start-Process @start
        $code = $process.ExitCode
    } catch {
        Say "could not start: $_"
        $code = -1
    }
    foreach ($file in @($out, $err)) {
        if (Test-Path $file) {
            $text = [System.IO.File]::ReadAllText($file)
            if ($text) { Say $text.TrimEnd() }
            Remove-Item $file -Force
        }
    }
    $seconds = [int]((Get-Date) - $started).TotalSeconds
    $line = "{0,-48} exit {1,4}  {2,5}s" -f $name, $code, $seconds
    $summary.Add($line)
    Say "----- $line"
    return $code
}

Set-Content -Path $results -Value "tau Windows test run $stamp" -Encoding UTF8
Say ("bundle: " + (Get-Content (Join-Path $here 'BUNDLE') -Raw).Trim())
Say ("windows: " + [System.Environment]::OSVersion.VersionString + ", " + $env:PROCESSOR_ARCHITECTURE)
Say ("powershell: " + $PSVersionTable.PSVersion)
Say ("user profile: " + $env:USERPROFILE)

# A zip from the network marks every file; uv.exe would ask before it runs.
Get-ChildItem -Path $here -Recurse -File | Unblock-File

$null = Step 'uv: install CPython' $uv @('python', 'install', $python)
$null = Step 'uv: make the venv' $uv @('venv', '--python', $python, '--seed', $venv)
$wheels = @(Get-ChildItem (Join-Path $here 'wheels') -Filter *.whl | ForEach-Object { $_.FullName })
$group = (Join-Path $src 'pyproject.toml') + ':dev'
$installed = Step 'uv: install the wheel and the test group' $uv (@('pip', 'install', '--python', $py, '--group', $group) + $wheels)
if ($installed -ne 0) {
    Say 'the install failed: nothing else can run'
} else {
    # 1. Smoke, in a throwaway TAU_HOME under the user's profile (its access list is the user's).
    $home_ = Join-Path $env:USERPROFILE ".tau-wintest-$stamp"
    New-Item -ItemType Directory -Path $home_ | Out-Null
    Set-Content -Path (Join-Path $home_ 'config.json') -Encoding ASCII -Value '{"default_model":"m","models":{"m":{"model":"m","provider":"mock","options":{"delay_ms":0}}}}'
    $env:TAU_HOME = $home_
    $tau = Join-Path $venv 'Scripts\tau.exe'
    $null = Step 'smoke: import tau._core' $py @('-c', 'import tau._core as m, sys; print(m.__file__, sys.version)')
    $null = Step 'smoke: tau --version' $tau @('--version')
    $null = Step 'smoke: tau -p (argument)' $tau @('-p', 'hello')
    $null = Step 'smoke: tau -p (piped stdin)' $tau @('-p', 'review') "piped text`n"
    $started = Step 'smoke: tau serve -d --json (new session)' $tau @('serve', '-d', '--json')
    if ($started -eq 0) {
        $null = Step 'smoke: tau -p -c (attached)' $tau @('-p', '-c', 'attached hello')
        $null = Step 'smoke: tau serve --stop -c' $tau @('serve', '--stop', '--json', '-c')
        $null = Step 'smoke: serve dir after stop' 'cmd.exe' @('/c', 'dir', '/b', (Join-Path $home_ 'serve'))
    }
    Remove-Item Env:\TAU_HOME

    # 2 and 3. pytest from outside the source tree, so the installed wheel is what is imported.
    $work = Join-Path $env:TEMP "tau-wintest-$stamp"
    New-Item -ItemType Directory -Path $work | Out-Null
    Push-Location $work
    try {
        $common = @('-m', 'pytest', '-p', 'no:cacheprovider', '-rfEs', '-m', 'not tokenizers')
        $serve = @((Join-Path $src 'tests\serve'), (Join-Path $src 'tests\cli\test_console_streams.py'), (Join-Path $src 'tests\cli\test_windows.py'))
        $null = Step 'pytest: serve and console' $py ($common + @('-v') + $serve)
        if (-not $Quick) {
            $null = Step 'pytest: the whole suite' $py ($common + @('-q', (Join-Path $src 'tests')))
        }
    } finally {
        Pop-Location
    }
}

Say ''
Say '===== summary'
foreach ($line in $summary) { Say $line }
Say ''
Say "results: $results"
Write-Host ''
Write-Host "Done. Bring back this file: $results"
