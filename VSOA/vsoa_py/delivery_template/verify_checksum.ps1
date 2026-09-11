$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($PSScriptRoot)
$prefix = $root.TrimEnd('\') + '\'
$failed = 0
foreach ($line in Get-Content -LiteralPath (Join-Path $root 'checksum.txt') -Encoding UTF8) {
    if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw "Invalid checksum line: $line" }
    $expected = $Matches[1]
    $relative = $Matches[2]
    $target = [System.IO.Path]::GetFullPath((Join-Path $root $relative))
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Checksum path escapes package' }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        Write-Host "MISSING $relative"
        $failed++
    } elseif ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        Write-Host "MISMATCH $relative"
        $failed++
    }
}
if ($failed -gt 0) { throw "$failed checksum mismatches; outputs/logs may change after running" }
Write-Host 'All packaged file checksums match.'
