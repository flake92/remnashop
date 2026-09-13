# Production secret preflight and data-store rotation

`make production-preflight` is read-only. It parses `.env` without sourcing
it, reports only variable names and policy violations, and never prints or
changes a secret. `_run_prod` depends on this check, so an invalid legacy
secret fails before Docker pulls or starts production services.

The check validates format, placeholders, strength and reuse. It deliberately
cannot prove that `DATABASE_PASSWORD` or `REDIS_PASSWORD` still matches a
credential inside an existing persistent volume. If either named volume
already exists, **do not replace its password only in `.env`**. Use the
coordinated procedure below. `make setup-env` is scaffolding for a fresh
installation; it preserves non-empty legacy values and neither validates nor
rotates an existing deployment.

The examples use the external-network compose file. Set `COMPOSE_FILE` to
`docker-compose.prod.internal.yml` when that is the deployed topology. Run the
commands from the deployment directory as the same privileged operator that
normally controls Docker. Disable shell tracing first so secrets cannot enter
the terminal log.

## Prepare a rollback point

Rotate PostgreSQL and Valkey one at a time. Do not delete or recreate their
volumes.

```sh
set +x
umask 077
COMPOSE_FILE=docker-compose.prod.external.yml
ROTATION_DIR="$(mktemp -d "${TMPDIR:-/tmp}/remnashop-secret-rotation.XXXXXX")"
cp -p .env "$ROTATION_DIR/env.before"
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" stop \
  remnashop remnashop-taskiq-worker remnashop-taskiq-scheduler
```

Copy the current values out of the rollback file without sourcing `.env` and
without displaying them:

```sh
python3 - "$ROTATION_DIR/env.before" "$ROTATION_DIR" <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
values = {}
for raw_line in source.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    values[name.strip()] = value
for name, filename in (
    ("DATABASE_PASSWORD", "database.old"),
    ("REDIS_PASSWORD", "valkey.old"),
):
    value = values.get(name)
    if not value:
        raise SystemExit(f"{name} is missing from the rollback .env")
    path = target / filename
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
PY
openssl rand -hex 24 > "$ROTATION_DIR/database.next"
openssl rand -hex 24 > "$ROTATION_DIR/valkey.next"
```

Take the normal database backup and a Docker-volume/snapshot rollback point
required by your operating procedure before changing authentication. Keep
`$ROTATION_DIR` until both the application and workers have passed acceptance.

The following helper replaces exactly one existing `.env` key using a temporary
file in the same directory and `os.replace`; neither the old nor new value is
passed in a command-line argument:

```sh
replace_env_from_file() {
  python3 - .env "$1" "$2" <<'PY'
import os
import pathlib
import stat
import sys
import tempfile

env_path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
secret_path = pathlib.Path(sys.argv[3])
secret = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
lines = env_path.read_text(encoding="utf-8").splitlines()
matches = [index for index, line in enumerate(lines) if line.startswith(f"{name}=")]
if len(matches) != 1:
    raise SystemExit(f"expected exactly one {name} entry in .env")
lines[matches[0]] = f"{name}={secret}"
mode = stat.S_IMODE(env_path.stat().st_mode)
fd, temporary_name = tempfile.mkstemp(prefix=".env.rotate.", dir=env_path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary_name, mode)
    os.replace(temporary_name, env_path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
}
```

## Rotate the PostgreSQL role password

The official PostgreSQL image uses `POSTGRES_PASSWORD` only when initializing
an empty data directory. Therefore changing `.env` or recreating the container
does not update the role in an existing `remnashop-db-data` volume.

1. Change the live role through the local Unix socket. The generated value is
   hex and is delivered on stdin, not in Docker or `psql` arguments:

```sh
cat "$ROTATION_DIR/database.next" | docker exec -i remnashop-db sh -ceu '
  IFS= read -r next_password
  case "$next_password" in (*[!0-9a-f]*|"") exit 2;; esac
  escaped_role=$(printf "%s" "$POSTGRES_USER" | sed '"'"'s/"/""/g'"'"')
  printf '"'"'ALTER ROLE "%s" PASSWORD '"'"'"'"'"'"'%s'"'"'"'"'"'"';\n'"'"' \
    "$escaped_role" "$next_password" |
    psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
'
```

2. Verify the new password over TCP before changing `.env`:

```sh
cat "$ROTATION_DIR/database.next" | docker exec -i remnashop-db sh -ceu '
  IFS= read -r PGPASSWORD
  export PGPASSWORD
  psql --set=ON_ERROR_STOP=1 --host 127.0.0.1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --command "SELECT 1" | grep -q 1
'
```

3. Atomically update `.env`, recreate only the database container so its
   declared environment matches reality, then run the read-only preflight:

```sh
replace_env_from_file DATABASE_PASSWORD "$ROTATION_DIR/database.next"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate remnashop-db
docker compose -f "$COMPOSE_FILE" exec -T remnashop-db \
  pg_isready -U "${DATABASE_USER:-remnashop}" -d "${DATABASE_NAME:-remnashop}"
make production-preflight
```

### PostgreSQL rollback

If any step fails, keep application consumers stopped. Restore the role via the
local socket, atomically restore only `DATABASE_PASSWORD`, and recreate only the
database container. Updating only that key avoids undoing a separately completed
Valkey rotation:

```sh
cat "$ROTATION_DIR/database.old" | docker exec -i remnashop-db sh -ceu '
  IFS= read -r old_password
  escaped_role=$(printf "%s" "$POSTGRES_USER" | sed '"'"'s/"/""/g'"'"')
  escaped_password=$(printf "%s" "$old_password" | sed "s/'"'"'/'"'"''"'"'/g")
  printf '"'"'ALTER ROLE "%s" PASSWORD '"'"'"'"'"'"'%s'"'"'"'"'"'"';\n'"'"' \
    "$escaped_role" "$escaped_password" |
    psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
'
replace_env_from_file DATABASE_PASSWORD "$ROTATION_DIR/database.old"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate remnashop-db
```

## Rotate the Valkey password

Valkey takes `--requirepass` from the compose command. Change the live process
first, verify the new credential, then atomically update `.env` and recreate
the same container. `valkey-cli -x` reads the new password from stdin rather
than an argument.

```sh
old_password=$(cat "$ROTATION_DIR/valkey.old")
next_password=$(cat "$ROTATION_DIR/valkey.next")
printf '%s\n%s\n' "$old_password" "$next_password" |
  docker exec -i remnashop-redis sh -ceu '
    IFS= read -r old_password
    IFS= read -r next_password
    test -n "$old_password" && test -n "$next_password"
    printf %s "$next_password" |
      REDISCLI_AUTH="$old_password" valkey-cli --no-auth-warning \
        -x CONFIG SET requirepass
  '
unset old_password next_password
cat "$ROTATION_DIR/valkey.next" | docker exec -i remnashop-redis sh -ceu '
  IFS= read -r REDISCLI_AUTH
  export REDISCLI_AUTH
  test "$(valkey-cli --no-auth-warning PING)" = PONG
'
replace_env_from_file REDIS_PASSWORD "$ROTATION_DIR/valkey.next"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate remnashop-redis
make production-preflight
```

### Valkey rollback

If the live Valkey process is still running, authenticate with the new value
and restore the old one. Then restore `.env` and recreate only Valkey. If the
process is already stopped, restore `.env` first and recreate it; the compose
`--requirepass` command restores the old credential.

```sh
next_password=$(cat "$ROTATION_DIR/valkey.next")
old_password=$(cat "$ROTATION_DIR/valkey.old")
printf '%s\n%s\n' "$next_password" "$old_password" |
  docker exec -i remnashop-redis sh -ceu '
    IFS= read -r next_password
    IFS= read -r old_password
    test -n "$next_password" && test -n "$old_password"
    printf %s "$old_password" |
      REDISCLI_AUTH="$next_password" valkey-cli --no-auth-warning \
        -x CONFIG SET requirepass
  ' || true
unset next_password old_password
replace_env_from_file REDIS_PASSWORD "$ROTATION_DIR/valkey.old"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate remnashop-redis
```

## Complete the maintenance window

After either successful rotation, start the one-shot migration and all runtime
services from the pinned image, then verify application, worker and scheduler
health before removing the rollback material:

```sh
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans --wait --wait-timeout 600
docker compose -f "$COMPOSE_FILE" ps
make production-preflight
```

Only after functional acceptance should the root-only rollback directory be
securely removed according to the host's storage policy.
