#!/usr/bin/env bash
set -e

echo "[sopds] Waiting for database at ${DB_HOST}:${DB_PORT}..."
python - <<'PY'
import os
import time
import psycopg

dsn = {
    "host": os.environ["DB_HOST"],
    "port": os.environ["DB_PORT"],
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASS"],
}
for attempt in range(60):
    try:
        conn = psycopg.connect(**dsn)
        conn.close()
        break
    except psycopg.OperationalError:
        if attempt == 59:
            raise
        time.sleep(2)
print("[sopds] Database is ready.")
PY

echo "[sopds] Applying migrations..."
python manage.py migrate --noinput

echo "[sopds] Collecting static files..."
python manage.py collectstatic --noinput

if [ -n "${ADMIN_USER}" ] && [ -n "${ADMIN_PASSWORD}" ]; then
    echo "[sopds] Creating admin user '${ADMIN_USER}'..."
    python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='${ADMIN_USER}').exists():
    User.objects.create_superuser('${ADMIN_USER}', '${ADMIN_EMAIL:-}', '${ADMIN_PASSWORD}')
    print('Admin user created.')
else:
    print('Admin user already exists.')
"
fi

echo "[sopds] Starting: $*"
exec "$@"
