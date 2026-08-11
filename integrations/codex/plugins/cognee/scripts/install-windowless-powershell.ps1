[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:USERPROFILE ".codex\bin\pwsh.exe"),
    [switch]$Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$owner = "cognee-codex-windowless-powershell"
$destinationPath = [IO.Path]::GetFullPath($Destination)
$markerPath = "$destinationPath.cognee-owner.json"
$pluginRoot = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $pluginRoot "windows\WindowlessPowerShell.cs"
$manifestPath = Join-Path $pluginRoot ".codex-plugin\plugin.json"

function Get-OwnedMarker {
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        return $null
    }

    try {
        $marker = Get-Content -Raw -LiteralPath $markerPath | ConvertFrom-Json
    }
    catch {
        throw "The existing launcher marker is unreadable: $markerPath"
    }

    if ($marker.owner -ne $owner) {
        throw "The existing launcher marker is not owned by Cognee: $markerPath"
    }
    return $marker
}

function Assert-OwnedDestination {
    if (-not (Test-Path -LiteralPath $destinationPath -PathType Leaf)) {
        return Get-OwnedMarker
    }

    $marker = Get-OwnedMarker
    if ($null -eq $marker) {
        throw "Refusing to overwrite an unowned pwsh executable: $destinationPath"
    }

    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $destinationPath).Hash
    if ($actualHash -ne $marker.sha256) {
        throw "Refusing to overwrite a modified Cognee pwsh executable: $destinationPath"
    }
    return $marker
}

if ($Remove) {
    $ownedMarker = Assert-OwnedDestination
    $removed = $false
    if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
        Remove-Item -LiteralPath $destinationPath -Force
        $removed = $true
    }
    if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        Remove-Item -LiteralPath $markerPath -Force
    }

    [pscustomobject]@{
        removed = $removed
        path = $destinationPath
        marker = $markerPath
        owner = $owner
    } | ConvertTo-Json
    return
}

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Windowless PowerShell source is missing: $sourcePath"
}
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Cognee plugin manifest is missing: $manifestPath"
}

$compilerCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$compiler = $null
foreach ($candidate in $compilerCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $compiler = $candidate
        break
    }
}
if (-not $compiler) {
    throw "The Windows .NET Framework C# compiler is unavailable."
}

$existingMarker = Assert-OwnedDestination
$version = (Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json).version
$sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourcePath).Hash
if ($null -ne $existingMarker -and
    (Test-Path -LiteralPath $destinationPath -PathType Leaf) -and
    $existingMarker.version -eq $version -and
    $existingMarker.source_sha256 -eq $sourceHash) {
    [pscustomobject]@{
        path = $destinationPath
        marker = $markerPath
        owner = $owner
        version = $version
        sha256 = $existingMarker.sha256
        source_sha256 = $sourceHash
    } | ConvertTo-Json
    return
}

$destinationParent = Split-Path -Parent $destinationPath
New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null

$runId = [Guid]::NewGuid().ToString("N")
$stageExecutable = Join-Path $destinationParent "pwsh.cognee-$runId.tmp.exe"
$stageMarker = Join-Path $destinationParent "pwsh.cognee-$runId.tmp.json"
$backupExecutable = Join-Path $destinationParent "pwsh.cognee-$runId.backup.exe"
$backupMarker = Join-Path $destinationParent "pwsh.cognee-$runId.backup.json"
$utf8NoBom = New-Object Text.UTF8Encoding($false)

try {
    $compilerOutput = @(
        & $compiler /nologo /target:winexe /optimize+ "/out:$stageExecutable" $sourcePath
    )
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $stageExecutable)) {
        throw "Could not compile the windowless PowerShell launcher: $($compilerOutput -join ' ')"
    }

    $installedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $stageExecutable).Hash
    $marker = [ordered]@{
        owner = $owner
        version = $version
        sha256 = $installedHash
        source_sha256 = $sourceHash
        installed_utc = [DateTime]::UtcNow.ToString("o")
    }
    [IO.File]::WriteAllText(
        $stageMarker,
        ($marker | ConvertTo-Json),
        $utf8NoBom
    )

    if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
        Move-Item -LiteralPath $destinationPath -Destination $backupExecutable
    }
    if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        Move-Item -LiteralPath $markerPath -Destination $backupMarker
    }

    try {
        Move-Item -LiteralPath $stageExecutable -Destination $destinationPath
        Move-Item -LiteralPath $stageMarker -Destination $markerPath
    }
    catch {
        if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
            Remove-Item -LiteralPath $destinationPath -Force
        }
        if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
            Remove-Item -LiteralPath $markerPath -Force
        }
        if (Test-Path -LiteralPath $backupExecutable -PathType Leaf) {
            Move-Item -LiteralPath $backupExecutable -Destination $destinationPath
        }
        if (Test-Path -LiteralPath $backupMarker -PathType Leaf) {
            Move-Item -LiteralPath $backupMarker -Destination $markerPath
        }
        throw
    }

    if (Test-Path -LiteralPath $backupExecutable -PathType Leaf) {
        Remove-Item -LiteralPath $backupExecutable -Force
    }
    if (Test-Path -LiteralPath $backupMarker -PathType Leaf) {
        Remove-Item -LiteralPath $backupMarker -Force
    }

    [pscustomobject]@{
        path = $destinationPath
        marker = $markerPath
        owner = $owner
        version = $version
        sha256 = $installedHash
        source_sha256 = $sourceHash
    } | ConvertTo-Json
}
finally {
    foreach ($temporaryPath in @(
        $stageExecutable,
        $stageMarker,
        $backupExecutable,
        $backupMarker
    )) {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}
