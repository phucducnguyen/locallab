#!/usr/bin/env bash
# Lab Watch tracker setup on the GB10: Forgejo 16.0.6-rootless in container lw-tracker.
#
#   tracker/setup_forgejo.sh            (run from the repo root, as Patrick)
#
# Before the first run, place the admin password (one line) in
#   ~/.config/labwatch/tracker_admin_password   (chmod 600)
#
# Never pulls an image: the image must already be loaded (offline install).
# Binds the tracker to 127.0.0.1:3000 only. Writes the setup and bench tokens
# straight to chmod 600 files and never prints them. Idempotent: a second run
# changes nothing.
set -euo pipefail

NAME=lw-tracker
IMAGE=codeberg.org/forgejo/forgejo:16.0.6-rootless
ADMIN=labwatch
SECRETS="${LW_SECRETS_DIR:-$HOME/.config/labwatch}"
DATA="${LW_TRACKER_HOME:-$HOME/lw-tracker}"
BASE="${LW_TRACKER:-http://127.0.0.1:3000}"
PASSFILE="$SECRETS/tracker_admin_password"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf 'setup_forgejo: %s\n' "$*"; }
die() { printf 'setup_forgejo: %s\n' "$*" >&2; exit 1; }

umask 077
mkdir -p "$SECRETS" "$DATA/data" "$DATA/conf"
chmod 700 "$SECRETS"

# 1. The image: present already, or stop. Never pull.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    say "image $IMAGE: present"
elif docker container inspect "$NAME" >/dev/null 2>&1; then
    say "image $IMAGE: not tagged, but container $NAME exists; using it"
else
    die "image $IMAGE is not loaded and container $NAME does not exist; load the image (docker load) first. This script never pulls."
fi

# 2. The container: reuse, start, or create.
if docker container inspect "$NAME" >/dev/null 2>&1; then
    if [ "$(docker container inspect -f '{{.State.Running}}' "$NAME")" = "true" ]; then
        say "container $NAME: running"
    else
        docker start "$NAME" >/dev/null
        say "container $NAME: started"
    fi
else
    docker run -d --pull never --name "$NAME" -u "$(id -u):$(id -g)" \
        -p 127.0.0.1:3000:3000 \
        -v "$DATA/data:/var/lib/gitea" -v "$DATA/conf:/etc/gitea" \
        -e FORGEJO__security__INSTALL_LOCK=true \
        -e FORGEJO__database__DB_TYPE=sqlite3 \
        -e FORGEJO__server__ROOT_URL=http://127.0.0.1:3000/ \
        -e FORGEJO__service__DISABLE_REGISTRATION=true \
        "$IMAGE" >/dev/null
    say "container $NAME: created"
fi

# 3. Wait for the API (local only).
for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "$BASE/api/v1/version" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS --max-time 2 "$BASE/api/v1/version" >/dev/null 2>&1 || die "no answer from $BASE/api/v1/version after 60 s"
say "api: $(curl -fsS --max-time 2 "$BASE/api/v1/version")"

# 4. The admin user.
ADMINS="$(docker exec "$NAME" forgejo admin user list --admin 2>/dev/null | awk 'NR>1 {print $2}')" \
    || die "could not list users in $NAME"
if printf '%s\n' "$ADMINS" | grep -qx "$ADMIN"; then
    say "user $ADMIN: exists"
else
    [ -s "$PASSFILE" ] || die "place the admin password in $PASSFILE (chmod 600) first"
    docker exec "$NAME" forgejo admin user create --admin --username "$ADMIN" \
        --password "$(head -n1 "$PASSFILE")" --email lw@example.invalid \
        --must-change-password=false >/dev/null
    say "user $ADMIN: created"
fi

# 5. Tokens: generated once, written straight to a chmod 600 file, never printed.
make_token() {  # name scopes file
    local tname="$1" scopes="$2" file="$3" tmp
    if [ -s "$file" ]; then
        say "token $tname: $file exists"
        return
    fi
    tmp="$file.tmp"
    rm -f -- "$tmp"
    if ! docker exec "$NAME" forgejo admin user generate-access-token --username "$ADMIN" \
            --token-name "$tname" --scopes "$scopes" --raw >"$tmp" 2>/dev/null \
            || ! [ -s "$tmp" ]; then
        rm -f -- "$tmp"
        die "could not generate token $tname (if a token of that name already exists in Forgejo but $file is gone, delete it in the web UI as $ADMIN, then rerun)"
    fi
    chmod 600 "$tmp"
    mv -f -- "$tmp" "$file"
    say "token $tname: written to $file"
}
make_token setup write:user,write:repository,write:issue "$SECRETS/tracker_setup_token"
make_token bench write:issue "$SECRETS/tracker_token"

# 6. Repo bench-1, labels, label ids (with the setup token).
cd "$REPO_ROOT"
LW_TRACKER="$BASE" LW_SECRETS_DIR="$SECRETS" python3 -m tracker.setup_api --secrets-dir "$SECRETS"
say "done"
