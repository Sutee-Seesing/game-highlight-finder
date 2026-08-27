[CmdletBinding()]
param(
    [string]$EnvPath,
    [string]$OutputPath,
    [string]$KeyPath,
    [switch]$ForceNewKey
)

$ErrorActionPreference = 'Stop'
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

if (-not $EnvPath) { $EnvPath = Join-Path $RepoRoot '.env' }
if (-not $OutputPath) { $OutputPath = Join-Path $RepoRoot 'secrets\env.enc.json' }
if (-not $KeyPath) {
    if ($env:GHF_ENV_BACKUP_KEY_FILE) { $KeyPath = $env:GHF_ENV_BACKUP_KEY_FILE }
    else { $KeyPath = Join-Path $env:USERPROFILE '.ghf\secrets\game-highlight-finder.env-backup.key' }
}

$EnvPath = [IO.Path]::GetFullPath($EnvPath)
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$KeyPath = [IO.Path]::GetFullPath($KeyPath)

if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
    throw "Environment file not found: $EnvPath"
}

Push-Location $RepoRoot
try {
    & git check-ignore -q -- '.env'
    if ($LASTEXITCODE -ne 0) { throw 'Safety check failed: repository .env is not ignored by Git.' }
}
finally { Pop-Location }

function Get-MasterKey {
    param([string]$Path, [switch]$Regenerate)

    if ($env:GHF_ENV_BACKUP_KEY) {
        try { $bytes = [Convert]::FromBase64String($env:GHF_ENV_BACKUP_KEY.Trim()) }
        catch { throw 'GHF_ENV_BACKUP_KEY is not valid Base64.' }
        if ($bytes.Length -ne 64) { throw 'GHF_ENV_BACKUP_KEY must decode to exactly 64 bytes.' }
        return ,$bytes
    }

    if ((Test-Path -LiteralPath $Path -PathType Leaf) -and -not $Regenerate) {
        try { $bytes = [Convert]::FromBase64String(([IO.File]::ReadAllText($Path)).Trim()) }
        catch { throw "Key file is not valid Base64: $Path" }
        if ($bytes.Length -ne 64) { throw "Key file must decode to exactly 64 bytes: $Path" }
        return ,$bytes
    }

    $bytes = New-Object byte[] 64
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $parent = Split-Path -Parent $Path
    if ($parent) { [IO.Directory]::CreateDirectory($parent) | Out-Null }
    [IO.File]::WriteAllText($Path, [Convert]::ToBase64String($bytes), (New-Object Text.UTF8Encoding($false)))
    return ,$bytes
}

$masterKey = Get-MasterKey -Path $KeyPath -Regenerate:$ForceNewKey
$aesKey = New-Object byte[] 32
$macKey = New-Object byte[] 32
[Buffer]::BlockCopy($masterKey, 0, $aesKey, 0, 32)
[Buffer]::BlockCopy($masterKey, 32, $macKey, 0, 32)

$plain = [IO.File]::ReadAllBytes($EnvPath)
$iv = New-Object byte[] 16
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($iv)

$aes = [Security.Cryptography.Aes]::Create()
$aes.KeySize = 256
$aes.BlockSize = 128
$aes.Mode = [Security.Cryptography.CipherMode]::CBC
$aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
$aes.Key = $aesKey
$aes.IV = $iv
$encryptor = $aes.CreateEncryptor()
try { $ciphertext = $encryptor.TransformFinalBlock($plain, 0, $plain.Length) }
finally {
    $encryptor.Dispose()
    $aes.Dispose()
}

$aadText = 'game-highlight-finder/.env:v1'
$aad = [Text.Encoding]::UTF8.GetBytes($aadText)
$macInput = New-Object byte[] ($aad.Length + $iv.Length + $ciphertext.Length)
[Buffer]::BlockCopy($aad, 0, $macInput, 0, $aad.Length)
[Buffer]::BlockCopy($iv, 0, $macInput, $aad.Length, $iv.Length)
[Buffer]::BlockCopy($ciphertext, 0, $macInput, ($aad.Length + $iv.Length), $ciphertext.Length)

$hmac = New-Object Security.Cryptography.HMACSHA256 -ArgumentList (,$macKey)
try { $tag = $hmac.ComputeHash($macInput) }
finally { $hmac.Dispose() }

$payload = [ordered]@{
    version = 1
    algorithm = 'AES-256-CBC+HMAC-SHA256'
    aad = $aadText
    createdAtUtc = [DateTime]::UtcNow.ToString('o')
    iv = [Convert]::ToBase64String($iv)
    hmac = [Convert]::ToBase64String($tag)
    ciphertext = [Convert]::ToBase64String($ciphertext)
}

$outputParent = Split-Path -Parent $OutputPath
if ($outputParent) { [IO.Directory]::CreateDirectory($outputParent) | Out-Null }
[IO.File]::WriteAllText($OutputPath, ($payload | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))

[Array]::Clear($plain, 0, $plain.Length)
[Array]::Clear($masterKey, 0, $masterKey.Length)
[Array]::Clear($aesKey, 0, $aesKey.Length)
[Array]::Clear($macKey, 0, $macKey.Length)

Write-Output "ENV_BACKUP_OK encrypted=$OutputPath"
if (-not $env:GHF_ENV_BACKUP_KEY) { Write-Output "ENV_BACKUP_KEY_OUTSIDE_GIT path=$KeyPath" }
else { Write-Output 'ENV_BACKUP_KEY_SOURCE=GHF_ENV_BACKUP_KEY' }
