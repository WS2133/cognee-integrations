[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = (& git -C $PSScriptRoot rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repositoryRoot) {
    throw "Could not resolve the Cognee integrations repository."
}

$pluginRelative = "integrations/codex/plugins/cognee"
$dirtyPluginFiles = @(& git -C $repositoryRoot status --porcelain -- $pluginRelative)
if ($LASTEXITCODE -ne 0 -or $dirtyPluginFiles.Count -gt 0) {
    throw "Commit the Cognee plugin changes before installing them."
}

$manifestText = (& git -C $repositoryRoot show "HEAD:$pluginRelative/.codex-plugin/plugin.json") -join "`n"
if ($LASTEXITCODE -ne 0) {
    throw "Could not read the committed plugin manifest."
}
$version = ($manifestText | ConvertFrom-Json).version
if (-not $version) {
    throw "The committed plugin manifest has no version."
}

$personalPluginRoot = Join-Path $env:USERPROFILE "plugins\cognee"
$cacheRoot = Join-Path $env:USERPROFILE ".codex\plugins\cache\personal\cognee"
$cacheTarget = Join-Path $cacheRoot $version
$personalParent = Split-Path -Parent $personalPluginRoot
$runId = [Guid]::NewGuid().ToString("N")
$stageRoot = Join-Path $personalParent ".cognee-stage-$runId"
$backupRoot = Join-Path $personalParent ".cognee-backup-$runId"
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) "cognee-plugin-$runId"
$archivePath = Join-Path $tempRoot "cognee.zip"
$expandedRoot = Join-Path $tempRoot "expanded"

New-Item -ItemType Directory -Path $personalParent, $cacheRoot, $tempRoot, $expandedRoot -Force | Out-Null

try {
    & git -C $repositoryRoot archive --format=zip "--output=$archivePath" "HEAD:$pluginRelative"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not package the committed Cognee plugin."
    }

    Expand-Archive -LiteralPath $archivePath -DestinationPath $expandedRoot
    Move-Item -LiteralPath $expandedRoot -Destination $stageRoot

    if (Test-Path -LiteralPath $personalPluginRoot) {
        Move-Item -LiteralPath $personalPluginRoot -Destination $backupRoot
    }

    try {
        Move-Item -LiteralPath $stageRoot -Destination $personalPluginRoot
    }
    catch {
        if (Test-Path -LiteralPath $backupRoot) {
            Move-Item -LiteralPath $backupRoot -Destination $personalPluginRoot
        }
        throw
    }

    if (-not (Test-Path -LiteralPath $cacheTarget)) {
        Copy-Item -LiteralPath $personalPluginRoot -Destination $cacheTarget -Recurse
    }

    $installedVersion = (
        Get-Content -Raw -LiteralPath (Join-Path $personalPluginRoot ".codex-plugin\plugin.json") |
            ConvertFrom-Json
    ).version
    if ($installedVersion -ne $version) {
        throw "Installed plugin version '$installedVersion' does not match '$version'."
    }

    if (Test-Path -LiteralPath $backupRoot) {
        Remove-Item -LiteralPath $backupRoot -Recurse -Force
    }

    [pscustomobject]@{
        commit = (& git -C $repositoryRoot rev-parse HEAD).Trim()
        version = $version
        source = $personalPluginRoot
        cache = $cacheTarget
    } | ConvertTo-Json
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
