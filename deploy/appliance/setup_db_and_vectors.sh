#!/bin/bash
set -e

mkdir -p /var/run/postgresql
chown -R postgres:postgres /var/run/postgresql
chmod 775 /var/run/postgresql

echo "=== Initializing PostgreSQL ==="
su - postgres -c "initdb -D /var/lib/pgsql/data"
echo "host all all 0.0.0.0/0 md5" >> /var/lib/pgsql/data/pg_hba.conf
echo "host all all 127.0.0.1/32 trust" >> /var/lib/pgsql/data/pg_hba.conf
echo "listen_addresses = '*'" >> /var/lib/pgsql/data/postgresql.conf
echo "unix_socket_directories = '/var/run/postgresql, /tmp'" >> /var/lib/pgsql/data/postgresql.conf

echo "=== Starting PostgreSQL for initial restore ==="
su - postgres -c "pg_ctl start -D /var/lib/pgsql/data -l /var/lib/pgsql/data/server.log -w" || { cat /var/lib/pgsql/data/server.log; exit 1; }

su - postgres -c "psql -c \"CREATE USER karaoke WITH PASSWORD 'karaoke' SUPERUSER;\""
su - postgres -c "psql -c \"CREATE DATABASE karaoke OWNER karaoke;\""

echo "=== Restoring complete PostgreSQL dump ==="
su - postgres -c "pg_restore -d karaoke /tmp/karaoke_complete_backup.dump || true"

echo "=== Starting OpenSearch for initial restore ==="
su - opensearch -c "/opt/opensearch/bin/opensearch -d"

echo "=== Waiting for OpenSearch to be ready ==="
for i in $(seq 1 60); do
    if curl -s http://localhost:9200 >/dev/null 2>&1; then
        echo "OpenSearch is online!"
        break
    fi
    sleep 2
done

echo "=== Restoring OpenSearch indices and vectors from PostgreSQL snapshots ==="
export KARAOKE_PG_URL="postgresql://karaoke:karaoke@localhost:5432/karaoke"
export OPENSEARCH_URL="http://localhost:9200"
export PYTHONPATH="/opt/karaoke/src"

/opt/karaoke/.venv/bin/python /opt/karaoke/scripts/backup_opensearch_to_postgres.py --restore

echo "=== Shutting down temporary background services ==="
pkill -f opensearch || true
su - postgres -c "pg_ctl stop -D /var/lib/pgsql/data -m fast" || true

echo "=== Seeding complete! Database and OpenSearch are fully populated. ==="
