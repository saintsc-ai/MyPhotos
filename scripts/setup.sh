#!/usr/bin/env bash
# MyPhotos — Docker setup wizard (Linux / macOS / Synology shell)
#
# One-shot interactive installer. Mirror of scripts/setup.ps1; same prompts,
# same outputs (.env, config-docker/, optional override). Re-runnable.
#
#   cd <repo>
#   ./scripts/setup.sh
#
# Steps:
#   1. Verify docker + compose plugin available and engine reachable.
#   2. If .env exists, offer reuse / overwrite.
#   3. Ask:
#        - photo source (local folder | SMB share | NFS share)
#        - SMB credentials (password via read -s so it stays off the screen)
#        - host port (default 8888)
#        - timezone (defaults to /etc/timezone or Asia/Seoul)
#   4. Write .env + config-docker/local.toml + (NAS only) override file.
#   5. docker compose pull
#   6. docker compose up -d
#   7. Poll /healthz; print a click-this URL when ready.

set -euo pipefail

# --------------------------------------------------------------- prettiers
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
warn() { printf '    \033[33m%s\033[0m\n' "$*"; }
err()  { printf '    \033[31m%s\033[0m\n' "$*" >&2; }

ask() {
    # ask "Prompt" "default" -> echoes user input or default
    local prompt="$1" def="${2:-}" val
    if [[ -n "$def" ]]; then
        printf '%s [%s]: ' "$prompt" "$def" >&2
    else
        printf '%s: ' "$prompt" >&2
    fi
    read -r val
    echo "${val:-$def}"
}

ask_yn() {
    # ask_yn "Question?" "y" -> 0 if yes, 1 if no
    local prompt="$1" def="${2:-y}" hint val
    [[ "$def" == "y" ]] && hint='[Y/n]' || hint='[y/N]'
    while true; do
        printf '%s %s: ' "$prompt" "$hint" >&2
        read -r val
        val="${val:-$def}"
        case "${val,,}" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
        esac
    done
}

gen_secret() {
    # 48 random bytes → base64url. python is the most portable since this
    # repo already ships a Python venv on every host that runs MyPhotos
    # natively; fall back to openssl, then /dev/urandom + tr.
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
    elif command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 48 | tr '+/' '-_' | tr -d '='
    else
        head -c 48 /dev/urandom | base64 | tr '+/' '-_' | tr -d '='
    fi
}

# --------------------------------------------------------------- locate repo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

printf '\033[1mMyPhotos Docker setup wizard\033[0m\n'
echo "Repo: $REPO_ROOT"

# --------------------------------------------------------------- 1) docker
say "Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    err "docker not found on PATH. Install Docker (or Docker Desktop) first."
    exit 1
fi
ok "docker CLI: $(docker --version)"

if ! docker info >/dev/null 2>&1; then
    err "docker engine not reachable. Start the daemon (or Docker Desktop) and re-run."
    exit 1
fi
ok "docker engine is running."

if ! docker compose version >/dev/null 2>&1; then
    err "docker compose plugin missing. Install docker-compose-plugin."
    exit 1
fi
ok "compose: $(docker compose version)"

# --------------------------------------------------------------- 2) reuse?
REUSE=0
if [[ -f .env ]]; then
    say ".env already exists"
    if ask_yn "Use the existing .env (skip prompts, jump straight to bring-up)?" 'y'; then
        REUSE=1
    fi
fi

if [[ $REUSE -eq 0 ]]; then
    say "Photo library source"
    echo "    1) Local folder (e.g. /home/me/Pictures)"
    echo "    2) NAS / Synology SMB share (//host/share)"
    MODE="$(ask '    Pick 1 or 2' '1')"

    PHOTO_ROOT=""
    USE_SMB=0
    SMB_HOST=""
    SMB_SHARE=""
    SMB_USER=""
    SMB_PASS=""

    if [[ "$MODE" == "2" ]]; then
        USE_SMB=1
        SMB_HOST="$(ask 'NAS hostname or IP' '192.168.1.201')"
        SMB_SHARE="$(ask 'SMB share name (no leading slash)' 'photo')"
        SMB_USER="$(ask 'SMB username' "${USER:-$LOGNAME}")"
        # read -s suppresses the echo so the password never lands on screen
        # or in `history`.
        printf 'SMB password: ' >&2
        read -rs SMB_PASS
        printf '\n' >&2
        PHOTO_ROOT="./_photos_unused"
        mkdir -p "$REPO_ROOT/_photos_unused"
    else
        while true; do
            PHOTO_ROOT="$(ask 'Photo folder (absolute path)' "$HOME/Pictures")"
            if [[ -d "$PHOTO_ROOT" ]]; then
                break
            fi
            warn "Path doesn't exist."
            if ask_yn 'Create it now?' 'y'; then
                mkdir -p "$PHOTO_ROOT"
                ok "Created $PHOTO_ROOT"
                break
            fi
        done
    fi

    API_PORT="$(ask 'API port (host side)' '8888')"
    DEFAULT_TZ="$(cat /etc/timezone 2>/dev/null || echo 'Asia/Seoul')"
    TZ_VAL="$(ask 'Timezone' "$DEFAULT_TZ")"
    SECRET="$(gen_secret)"

    # ----- .env ----------------------------------------------------------
    say "Writing .env"
    {
        echo "# MyPhotos — generated by scripts/setup.sh"
        echo "# Re-run the wizard to regenerate; hand-edit anything you want to pin."
        echo ""
        echo "PHOTO_ROOT=$PHOTO_ROOT"
        echo "DATA_DIR=./data"
        echo "CONFIG_DIR=./config-docker"
        echo "API_PORT=$API_PORT"
        echo "TZ=$TZ_VAL"
        echo "APP_UID=$(id -u 2>/dev/null || echo 1000)"
        echo "APP_GID=$(id -g 2>/dev/null || echo 1000)"
        echo ""
        if [[ $USE_SMB -eq 1 ]]; then
            cat <<EOF
# NAS SMB credentials — read by docker-compose.override.yml.
# Treat .env as a secret (it's in .gitignore). Do NOT paste it in chat
# or screenshots.
SMB_HOST=$SMB_HOST
SMB_SHARE=$SMB_SHARE
SMB_USER=$SMB_USER
SMB_PASS=$SMB_PASS
EOF
        fi
    } > .env
    chmod 600 .env
    ok ".env written (mode 600)"

    # ----- config-docker -------------------------------------------------
    say "Writing config-docker/local.toml"
    mkdir -p config-docker
    [[ -f config/default.toml ]] && cp -f config/default.toml config-docker/default.toml
    cat > config-docker/local.toml <<EOF
# MyPhotos config (Docker mount target)
#
# Auto-generated by scripts/setup.sh. Adjust freely; re-running the wizard
# overwrites only when you let it.

[server]
host = "0.0.0.0"
port = 8888

[security]
secret_key = "$SECRET"
EOF
    ok "config-docker/local.toml written"

    # ----- override (SMB) ------------------------------------------------
    OVERRIDE_PATH="docker-compose.override.yml"
    if [[ $USE_SMB -eq 1 ]]; then
        say "Writing $OVERRIDE_PATH (cifs mount)"
        cat > "$OVERRIDE_PATH" <<'EOF'
# Auto-generated by scripts/setup.sh. Mounts the NAS SMB share as the
# /photos volume in all three services. Edit ".env" (SMB_*) to change
# credentials; re-run the wizard to switch back to a local folder.

services:
  api:
    volumes:
      - photos:/photos:ro
  worker:
    volumes:
      - photos:/photos:ro
  ml-worker:
    volumes:
      - photos:/photos:ro

volumes:
  photos:
    driver: local
    driver_opts:
      type: cifs
      o: "username=${SMB_USER},password=${SMB_PASS},uid=1000,gid=1000,ro,vers=3.0,nounix,iocharset=utf8"
      device: "//${SMB_HOST}/${SMB_SHARE}"
EOF
        ok "override written"
    elif [[ -f "$OVERRIDE_PATH" ]]; then
        if ask_yn "Existing $OVERRIDE_PATH found — delete it (local-folder mode doesn't need it)?" 'y'; then
            rm -f "$OVERRIDE_PATH"
            ok "override deleted"
        fi
    fi

    unset SMB_PASS
fi

# --------------------------------------------------------------- 3) bring up
say "Pulling image (first run can take a minute)"
if ! docker compose pull; then
    warn "Pull failed or no image available — Compose will try to build on up."
fi

say "Starting the stack (docker compose up -d)"
if ! docker compose up -d; then
    err "Compose up failed. Run 'docker compose logs' to see why."
    exit 1
fi

# --------------------------------------------------------------- 4) poll
PORT="$(grep -E '^\s*API_PORT\s*=' .env | head -1 | sed 's/^[^=]*=\s*//' | tr -d '"' || echo 8888)"
PORT="${PORT:-8888}"
URL="http://127.0.0.1:${PORT}/healthz"

say "Waiting for $URL"
for _ in $(seq 60); do
    if curl -fsS -m 2 "$URL" >/dev/null 2>&1; then
        ok "API is healthy."
        echo ""
        echo "Open in browser:    http://127.0.0.1:${PORT}"
        echo "Watch logs:         docker compose logs -f api worker"
        echo "Stop the stack:     docker compose down"
        # macOS / Linux desktop best-effort: open the URL.
        if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true
        elif command -v open    >/dev/null 2>&1; then open    "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true
        fi
        exit 0
    fi
    sleep 1
done

err "API didn't respond in 60s. Show its log with:"
echo "   docker compose logs --tail 80 api" >&2
exit 1
