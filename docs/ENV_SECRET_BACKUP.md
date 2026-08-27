# Encrypted `.env` backup

This repository keeps the real `.env` file out of Git. A portable encrypted copy can be committed as `secrets/env.enc.json`.

## Security model

- Plaintext `.env` stays ignored by Git.
- The encrypted payload uses AES-256-CBC with HMAC-SHA256 (encrypt-then-MAC).
- The 64-byte master key is stored outside the repository by default at:
  `%USERPROFILE%\.ghf\secrets\game-highlight-finder.env-backup.key`
- The repository also ignores `secrets/*.key` and `*.env-backup.key` to reduce accidental key commits.
- Do not commit or paste the master key into the repository, issues, logs, or chat transcripts.

## Refresh the encrypted backup

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup-env.ps1
```

The command verifies that `.env` is ignored before reading it. It writes the encrypted payload to `secrets/env.enc.json` and reuses the existing master key unless `-ForceNewKey` is supplied.

## Restore on another Windows machine

1. Clone or pull the repository.
2. Transfer the master key to the new machine using a secure channel and place it at:
   `%USERPROFILE%\.ghf\secrets\game-highlight-finder.env-backup.key`
3. Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore-env.ps1
```

If `.env` already exists, the restore script refuses to overwrite it unless `-Force` is explicitly supplied.

## Alternate key sources

Instead of the default key file, either of these may be used:

- `GHF_ENV_BACKUP_KEY_FILE` — path to a Base64-encoded 64-byte master key file.
- `GHF_ENV_BACKUP_KEY` — the Base64-encoded 64-byte master key directly in the process environment.

`GHF_ENV_BACKUP_KEY` should only be set for the current process/session when practical; avoid persisting it in shell history or repository files.

## If the master key is lost or exposed

- Lost key: the encrypted payload cannot be recovered; create a new backup from a machine that still has the plaintext `.env`.
- Exposed key: rotate affected provider/API credentials, generate a fresh master key with `backup-env.ps1 -ForceNewKey`, and commit the newly encrypted payload.
