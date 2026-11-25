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

# Kill a process and everything it started (taskkill: Windows PowerShell 5.1 has no Kill($true)).
function Stop-Tree([int]$id) {
    & taskkill.exe /T /F /PID $id *> $null
}

# Run a native command; its output (stdout and stderr) goes to the screen and the results file.
# Returns its exit code. A command that cannot start is an error code of -1, said in the file.
# It waits for that process alone, never its descendants (`Start-Process -Wait` in Windows
# PowerShell 5.1 waits for them too, and `tau serve -d` leaves a server running by design), and
# for at most $timeout seconds: then the process tree is killed, the summary says `exit TIMEOUT`
# and the code is -2.
function Step([string]$name, [string]$exe, [string[]]$arguments, [string]$stdin = $null, [int]$timeout = 120) {
    Say ''
    Say "===== $name"
    Say ("> " + $exe + ' ' + ($arguments -join ' '))
    $started = Get-Date
    $code = -1
    $timedOut = $false
    $outText = ''
    $errText = ''
    $process = $null
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $exe
        $info.Arguments = (($arguments | ForEach-Object { Quote $_ }) -join ' ')
        $info.WorkingDirectory = (Get-Location).Path
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.RedirectStandardInput = ($stdin -ne $null -and $stdin -ne '')
        $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $process = [System.Diagnostics.Process]::Start($info)
        # Read both pipes at once so a full pipe cannot stall the child.
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        if ($info.RedirectStandardInput) {
            $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($stdin)
            $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
            $process.StandardInput.Close()
        }
        if ($process.WaitForExit($timeout * 1000)) {
            $process.WaitForExit()
            $code = $process.ExitCode
        } else {
            $timedOut = $true
            $code = -2
            Stop-Tree $process.Id
            $null = $process.WaitForExit(10000)
        }
        # A descendant may still hold a pipe open: take what has arrived, do not wait for EOF.
        if ($outTask.Wait(5000)) { $outText = $outTask.Result }
        if ($errTask.Wait(5000)) { $errText = $errTask.Result }
    } catch {
        Say "could not start: $_"
        $code = -1
    }
    foreach ($text in @($outText, $errText)) {
        if ($text) { Say $text.TrimEnd() }
    }
    if ($timedOut) { Say "timed out after $timeout s: the process and its children were killed" }
    $seconds = [int]((Get-Date) - $started).TotalSeconds
    $shown = if ($timedOut) { 'TIMEOUT' } else { [string]$code }
    $line = "{0,-48} exit {1,7}  {2,5}s" -f $name, $shown, $seconds
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

$null = Step 'uv: install CPython' $uv @('python', 'install', $python) $null 900
$null = Step 'uv: make the venv' $uv @('venv', '--python', $python, '--seed', $venv) $null 600
$wheels = @(Get-ChildItem (Join-Path $here 'wheels') -Filter *.whl | ForEach-Object { $_.FullName })
$group = (Join-Path $src 'pyproject.toml') + ':dev'
$installed = Step 'uv: install the wheel and the test group' $uv (@('pip', 'install', '--python', $py, '--group', $group) + $wheels) $null 1800
if ($installed -ne 0) {
    Say 'the install failed: nothing else can run'
} else {
    # 1. Smoke, in a throwaway TAU_HOME under the user's profile (its access list is the user's).
    $home_ = Join-Path $env:USERPROFILE ".tau-wintest-$stamp"
    New-Item -ItemType Directory -Path $home_ | Out-Null
    Set-Content -Path (Join-Path $home_ 'config.json') -Encoding ASCII -Value '{"default_model":"m","models":{"m":{"model":"m","provider":"mock","options":{"delay_ms":0}}}}'
    $env:TAU_HOME = $home_
    $tau = Join-Path $venv 'Scripts\tau.exe'
    $stopped = $false
    try {
        $null = Step 'smoke: import tau._core' $py @('-c', 'import tau._core as m, sys; print(m.__file__, sys.version)')
        $null = Step 'smoke: tau --version' $tau @('--version')
        $null = Step 'smoke: tau -p (argument)' $tau @('-p', 'hello')
        $null = Step 'smoke: tau -p (piped stdin)' $tau @('-p', 'review') "piped text`n"
        $started = Step 'smoke: tau serve -d --json (new session)' $tau @('serve', '-d', '--json')
        if ($started -eq 0) {
            $null = Step 'smoke: tau -p -c (attached)' $tau @('-p', '-c', 'attached hello')
            $stop = Step 'smoke: tau serve --stop -c' $tau @('serve', '--stop', '--json', '-c')
            if ($stop -eq 0) { $stopped = $true }
            $null = Step 'smoke: serve dir after stop' 'cmd.exe' @('/c', 'dir', '/b', (Join-Path $home_ 'serve'))
        }
    } finally {
        # Whatever failed or timed out above, leave no server and no locked TAU_HOME behind.
        if (-not $stopped) {
            $null = Step 'cleanup: tau serve --stop -c' $tau @('serve', '--stop', '--json', '-c') $null 60
        }
        Remove-Item Env:\TAU_HOME
    }

    # 2 and 3. pytest from outside the source tree, so the installed wheel is what is imported.
    $work = Join-Path $env:TEMP "tau-wintest-$stamp"
    New-Item -ItemType Directory -Path $work | Out-Null
    Push-Location $work
    try {
        # --timeout (pytest-timeout): a hung test ends the run with every thread's stack, well
        # before the step's own timeout kills it with nothing said.
        $common = @('-m', 'pytest', '-p', 'no:cacheprovider', '-rfEs', '-m', 'not tokenizers', '--timeout', '300')
        $serve = @((Join-Path $src 'tests\serve'), (Join-Path $src 'tests\cli\test_console_streams.py'), (Join-Path $src 'tests\cli\test_windows.py'))
        $null = Step 'pytest: serve and console' $py ($common + @('-v') + $serve) $null 1800
        if (-not $Quick) {
            $null = Step 'pytest: the whole suite' $py ($common + @('-q', (Join-Path $src 'tests'))) $null 5400
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
