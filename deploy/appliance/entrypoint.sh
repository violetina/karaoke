#!/bin/bash
set -e

# Fix ownership if volumes were mounted externally
chown -R postgres:postgres /var/lib/pgsql/data || true
chmod 700 /var/lib/pgsql/data || true
chown -R opensearch:opensearch /opt/opensearch || true

echo "Starting Karaoke All-in-One Fedora Backend Services..."
exec /usr/bin/supervisord -c /etc/supervisord.conf
