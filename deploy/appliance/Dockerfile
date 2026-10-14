FROM fedora:40

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    KARAOKE_PG_URL=postgresql://karaoke:karaoke@localhost:5432/karaoke \
    OPENSEARCH_URL=http://localhost:9200 \
    OPENSEARCH_JAVA_OPTS="-Xms1g -Xmx1g"

# 1. System packages
RUN dnf install -y \
    postgresql-server \
    postgresql-contrib \
    java-17-openjdk-headless \
    python3 \
    python3-pip \
    python3-devel \
    gcc \
    git \
    ffmpeg-free \
    supervisor \
    procps-ng \
    curl \
    wget \
    tar \
    gzip \
    && dnf clean all

# 2. Install OpenSearch 2.18
RUN useradd -m -u 1000 opensearch && \
    mkdir -p /opt/opensearch && \
    curl -fsSL https://artifacts.opensearch.org/releases/bundle/opensearch/2.18.0/opensearch-2.18.0-linux-x64.tar.gz | tar -xz -C /opt/opensearch --strip-components=1 && \
    chown -R opensearch:opensearch /opt/opensearch

# Configure OpenSearch (disable security plugin for local dev, single node)
RUN echo "plugins.security.disabled: true" >> /opt/opensearch/config/opensearch.yml && \
    echo "discovery.type: single-node" >> /opt/opensearch/config/opensearch.yml && \
    echo "bootstrap.system_call_filter: false" >> /opt/opensearch/config/opensearch.yml && \
    echo "network.host: 0.0.0.0" >> /opt/opensearch/config/opensearch.yml

# 3. Setup Python Backend Environment
WORKDIR /opt/karaoke
RUN python3 -m venv /opt/karaoke/.venv
ENV PATH="/opt/karaoke/.venv/bin:$PATH"

COPY requirements.txt /opt/karaoke/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir \
        "uvicorn[standard]>=0.28.0" \
        "fastapi>=0.110.0" \
        "psycopg[binary,pool]>=3.0.0" \
        "opensearch-py>=2.5.0" \
        "yt-dlp>=2024.0"

# Copy application source & pyproject.toml
COPY pyproject.toml /opt/karaoke/
COPY src/ /opt/karaoke/src/
COPY scripts/ /opt/karaoke/scripts/
RUN pip install --no-cache-dir -e .

# 4. Copy Backup Artifacts and Restore Script
COPY build_vm/karaoke_complete_backup.dump /tmp/karaoke_complete_backup.dump
COPY deploy/appliance/setup_db_and_vectors.sh /setup_db_and_vectors.sh
RUN chmod +x /setup_db_and_vectors.sh && /setup_db_and_vectors.sh && rm -f /tmp/karaoke_complete_backup.dump

# 5. Supervisord configuration and Entrypoint
COPY deploy/appliance/supervisord.conf /etc/supervisord.conf
COPY deploy/appliance/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000 8888 5432 9200

ENTRYPOINT ["/entrypoint.sh"]
