# MyPhotos

Self-hosted photo catalog with metadata indexing and web browsing.

- **Backend**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **Worker**: separate process for scanning, EXIF extraction, thumbnail generation
- **Storage**: indexes existing folders read-only; thumbnails and DB live inside `data/`
- **Target host**: Synology DSM (DS3622xs+, x86_64) via systemd

## Layout

```
myphotos/
├── app/                # application code
│   ├── api/            # FastAPI app (uvicorn entry)
│   ├── admin/          # admin CRUD (roots, jobs)
│   ├── worker/         # scanner + job runner (systemd entry)
│   └── web/            # HTMX templates / static
├── config/
│   ├── default.toml    # built-in defaults (tracked)
│   └── local.toml      # per-host overrides (NOT tracked)
├── data/               # runtime (NOT tracked) — DB, thumbs, logs, trash
├── vendor/             # OS-specific binaries (exiftool, ffmpeg)
├── alembic/            # DB migrations
├── scripts/            # bootstrap, systemd install
└── systemd/            # unit templates
```

## Bootstrap (NAS)

Prerequisite: Python 3.11+ available. Recommended via [uv](https://docs.astral.sh/uv/):

```bash
# one-time uv install (user-local, no root needed)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv python install 3.11.9
```

Then:

```bash
git clone <repo> /var/services/homes/scsung/myphotos
cd /var/services/homes/scsung/myphotos
./scripts/bootstrap.sh                # detects uv automatically
# edit config/local.toml (secret_key at minimum)
./scripts/install-systemd.sh          # APP_USER=current user by default
sudo systemctl enable --now myphotos-api myphotos-worker
```

> Note: uv-created venvs do not include `pip` by default. Use `uv pip install ...`
> for ad-hoc installs, or `.venv/bin/python -m <module>` to run scripts.

## Bootstrap (Windows dev)

```powershell
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml
.\scripts\run-api.ps1     # in one terminal
.\scripts\run-worker.ps1  # in another
```

## Porting to a new host

1. Copy `data/` and `config/local.toml` to the new host
2. `git clone` this repo, run `bootstrap.sh`
3. Edit `config/local.toml` (cache path may differ)
4. In the admin UI, update each root's `abs_path` if photo folders moved
5. `install-systemd.sh` and start services

DB is a single SQLite file. No external services required.
