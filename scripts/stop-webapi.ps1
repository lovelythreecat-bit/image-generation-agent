param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable
)

$ErrorActionPreference = 'Stop'

try {
    $pythonPath = (Resolve-Path -LiteralPath $PythonExecutable).ProviderPath
    $processes = @(Get-CimInstance Win32_Process)
    $apiProcesses = @($processes | Where-Object {
        $_.ExecutablePath -ieq $pythonPath -and
        $_.CommandLine -match '(?i)(?:^|\s)-m\s+image_agent\.server(?:\s|$)' -and
        $_.CommandLine -notmatch '(?i)(?:^|\s)import-config(?:\s|$)'
    })

    foreach ($apiProcess in $apiProcesses) {
        # The virtualenv launcher can own a second Python process running the API.
        $processIds = @([int]$apiProcess.ProcessId)
        do {
            $children = @($processes | Where-Object {
                $_.ParentProcessId -in $processIds -and $_.ProcessId -notin $processIds
            })
            $processIds += @($children | ForEach-Object { [int]$_.ProcessId })
        } while ($children.Count -gt 0)

        Write-Host "Stopping existing Web API (PID $($apiProcess.ProcessId))..."
        # Stop children first, then wait for every captured process to exit.
        [array]::Reverse($processIds)
        $running = @($processIds | ForEach-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue
        })
        foreach ($process in $running) {
            if (-not $process.HasExited) {
                Stop-Process -InputObject $process -Force
            }
        }
        foreach ($process in $running) {
            if (-not $process.WaitForExit(10000)) {
                throw "Timed out waiting for Web API process $($process.Id) to exit."
            }
        }
    }
    exit 0
} catch {
    Write-Error "Unable to stop the existing Web API: $_"
    exit 1
}
