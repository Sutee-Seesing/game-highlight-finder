[CmdletBinding()]
param(
    [string]$InputPath,
    [string]$OutputPath,
    [string]$KeyPath,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

if (-not $InputPath) { $InputPath = Join-Path $RepoRoot 'secrets\env.enc.json' }
if (-not $OutputPath) { $OutputPath = Join-Path $RepoRoot '.env' }
if (-not $KeyPath) {
    if ($env:GHF_ENV_BACKUP_KEY_FILE) { $KeyPath = $env:GHF_ENV_BACKUP_KEY_FILE }
    else { $KeyPath = Join-Path $RepoRoot 'secrets\env-backup.key' }
}

$InputPath = [IO.Path]::GetFullPath($InputPath)
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$KeyPath = [IO.Path]::GetFullPath($KeyPath)

if (-not (Test-Path -LiteralPath $InputPath -PathType Leaf)) { throw "Encrypted backup not found: $InputPath" }
if ((Test-Path -LiteralPath $OutputPath) -and -not $Force) { throw "Output already exists: $OutputPath. Use -Force to replace it." }

Push-Location $RepoRoot
try {
    & git check-ignore -q -- '.env'
    if ($LASTEXITCODE -ne 0) { throw 'Safety check failed: repository .env is not ignored by Git.' }
}
finally { Pop-Location }

function Read-MasterKey {
    param([string]$Path)

    if ($env:GHF_ENV_BACKUP_KEY) {
        try { $bytes = [Convert]::FromBase64String($env:GHF_ENV_BACKUP_KEY.Trim()) }
        catch { throw 'GHF_ENV_BACKUP_KEY is not valid Base64.' }
        if ($bytes.Length -ne 64) { throw 'GHF_ENV_BACKUP_KEY must decode to exactly 64 bytes.' }
        return ,$bytes
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Backup key not found: $Path. Pull the repository key file or set GHF_ENV_BACKUP_KEY / GHF_ENV_BACKUP_KEY_FILE."
    }
    try { $bytes = [Convert]::FromBase64String(([IO.File]::ReadAllText($Path)).Trim()) }
    catch { throw "Key file is not valid Base64: $Path" }
    if ($bytes.Length -ne 64) { throw "Key file must decode to exactly 64 bytes: $Path" }
    return ,$bytes
}

$payload = Get-Content -LiteralPath $InputPath -Raw | ConvertFrom-Json
if ($payload.version -ne 1) { throw "Unsupported encrypted backup version: $($payload.version)" }
if ($payload.algorithm -ne 'AES-256-CBC+HMAC-SHA256') { throw "Unsupported algorithm: $($payload.algorithm)" }
if ($payload.aad -ne 'game-highlight-finder/.env:v1') { throw 'Encrypted backup context does not match this project.' }

$masterKey = Read-MasterKey -Path $KeyPath
$aesKey = New-Object byte[] 32
$macKey = New-Object byte[] 32
[Buffer]::BlockCopy($masterKey, 0, $aesKey, 0, 32)
[Buffer]::BlockCopy($masterKey, 32, $macKey, 0, 32)

try {
    $iv = [Convert]::FromBase64String([string]$payload.iv)
    $ciphertext = [Convert]::FromBase64String([string]$payload.ciphertext)
    $expectedTag = [Convert]::FromBase64String([string]$payload.hmac)
}
catch { throw 'Encrypted backup contains invalid Base64 fields.' }

if ($iv.Length -ne 16) { throw 'Encrypted backup IV length is invalid.' }
if ($expectedTag.Length -ne 32) { throw 'Encrypted backup HMAC length is invalid.' }

$aad = [Text.Encoding]::UTF8.GetBytes([string]$payload.aad)
$macInput = New-Object byte[] ($aad.Length + $iv.Length + $ciphertext.Length)
[Buffer]::BlockCopy($aad, 0, $macInput, 0, $aad.Length)
[Buffer]::BlockCopy($iv, 0, $macInput, $aad.Length, $iv.Length)
[Buffer]::BlockCopy($ciphertext, 0, $macInput, ($aad.Length + $iv.Length), $ciphertext.Length)

$hmac = New-Object Security.Cryptography.HMACSHA256 -ArgumentList (,$macKey)
try { $actualTag = $hmac.ComputeHash($macInput) }
finally { $hmac.Dispose() }

$diff = 0
for ($i = 0; $i -lt $actualTag.Length; $i++) { $diff = $diff -bor ($actualTag[$i] -bxor $expectedTag[$i]) }
if ($diff -ne 0) { throw 'Encrypted backup authentication failed. Wrong key or modified ciphertext.' }

$aes = [Security.Cryptography.Aes]::Create()
$aes.KeySize = 256
$aes.BlockSize = 128
$aes.Mode = [Security.Cryptography.CipherMode]::CBC
$aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
$aes.Key = $aesKey
$aes.IV = $iv
$decryptor = $aes.CreateDecryptor()
try { $plain = $decryptor.TransformFinalBlock($ciphertext, 0, $ciphertext.Length) }
finally {
    $decryptor.Dispose()
    $aes.Dispose()
}

$outputParent = Split-Path -Parent $OutputPath
if ($outputParent) { [IO.Directory]::CreateDirectory($outputParent) | Out-Null }
[IO.File]::WriteAllBytes($OutputPath, $plain)

[Array]::Clear($plain, 0, $plain.Length)
[Array]::Clear($masterKey, 0, $masterKey.Length)
[Array]::Clear($aesKey, 0, $aesKey.Length)
[Array]::Clear($macKey, 0, $macKey.Length)

Write-Output "ENV_RESTORE_OK output=$OutputPath"
