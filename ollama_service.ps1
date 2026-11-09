function Get-PrivateOllamaEnvironment {
    param([Parameter(Mandatory = $true)][string]$ModelsDirectory)

    # Apply identically in source setup and startup. Inherited debug settings
    # must not turn chat prompts into files or enable arbitrary browser origins.
    # Empty ORIGINS retains Ollama's built-in local/app origins, not an ACL.
    return @{
        OLLAMA_HOST = '127.0.0.1:11435'
        OLLAMA_NO_CLOUD = '1'
        OLLAMA_DEBUG = '0'
        OLLAMA_DEBUG_LOG_REQUESTS = '0'
        OLLAMA_ORIGINS = ''
        OLLAMA_MODELS = $ModelsDirectory
        # Short chat needs little context; one model/request avoids game-GPU contention.
        OLLAMA_CONTEXT_LENGTH = '2048'
        OLLAMA_NUM_PARALLEL = '1'
        OLLAMA_MAX_LOADED_MODELS = '1'
        OLLAMA_KEEP_ALIVE = '5m'
    }
}

function Find-LocalOllamaListener {
    param([object[]]$Connections, [int]$Port, [int]$ProbePort)

    # Correlate the server side of our live connection with its bound socket.
    # State is deliberately unused: Windows has reported LISTEN for stale
    # ownerless connections, and 0/blank for the real listener on this machine.
    $serviceOwners = @($Connections | Where-Object {
        $_.LocalAddress -eq '127.0.0.1' -and $_.LocalPort -eq $Port -and
        $_.RemoteAddress -eq '127.0.0.1' -and $_.RemotePort -eq $ProbePort -and
        $_.OwningProcess -gt 0
    } | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($serviceOwners.Count -ne 1) {
        throw "Windows could not verify the owner of the local Ollama connection on port $Port. Try starting the translator again."
    }
    $listeners = @($Connections | Where-Object {
        $_.OwningProcess -eq $serviceOwners[0] -and $_.LocalPort -eq $Port -and
        $_.RemoteAddress -in @('0.0.0.0', '::') -and $_.RemotePort -eq 0
    })
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') {
        throw "Windows could not verify an exclusively local Ollama listener on port $Port. Try starting the translator again."
    }
    return $listeners[0]
}

function Get-LocalOllamaListener {
    param([int]$Port = 11435)

    # Bound ownership probing separately from model-loading/API timeouts.
    $connectionTimeoutMilliseconds = 3000
    $probe = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $probe.ConnectAsync('127.0.0.1', $Port)
        if (-not $connection.Wait($connectionTimeoutMilliseconds)) {
            throw "The local Ollama connection on port $Port timed out. Try starting the translator again."
        }
        $probePort = $probe.Client.LocalEndPoint.Port
        $connections = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction Stop)
        return Find-LocalOllamaListener -Connections $connections -Port $Port -ProbePort $probePort
    } finally {
        $probe.Dispose()
    }
}
