# Portable `.env` backup for this private repository

This personal repository keeps the real `.env` file out of Git, but intentionally commits both an encrypted copy (`secrets/env.enc.json`) and its decryption key (`secrets/env-backup.key`) so the environment can be restored after cloning on another machine.

## Security model

- Plaintext `.env` stays ignored by Git.
- The encrypted payload uses AES-256-CBC with HMAC-SHA256 (encrypt-then-MAC).
- The 64-byte master key is intentionally tracked in this private repository at `secrets/env-backup.key`.
- Because the repository contains both ciphertext and its key, anyone who can read the repository can recover the `.env`. Treat repository access as equivalent to access to these credentials.
- Do not make the repository public or copy `secrets/env-backup.key` / the decrypted `.env` into issues, logs, or chat transcripts.

## Refresh the encrypted backup

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup-env.ps1
```

The command verifies that `.env` is ignored before reading it. By default it reuses `secrets/env-backup.key` and writes the encrypted payload to `secrets/env.enc.json`. Use `-ForceNewKey` only when intentionally rotating the repository backup key.

## Restore on another Windows machine

1. Clone or pull the repository.
2. Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore-env.ps1
```

The restore script uses `secrets/env-backup.key` automatically. If `.env` already exists, it refuses to overwrite it unless `-Force` is explicitly supplied.

## Alternate key sources

The repository key is the default, but either of these may override it:

- `GHF_ENV_BACKUP_KEY_FILE` — path to another Base64-encoded 64-byte master key file.
- `GHF_ENV_BACKUP_KEY` — the Base64-encoded 64-byte master key directly in the current process environment.

## If repository access or credentials are exposed

Rotate the affected provider/API credentials. If only the backup key needs rotation, run `backup-env.ps1 -ForceNewKey` and commit both the new `secrets/env-backup.key` and `secrets/env.enc.json` together.
