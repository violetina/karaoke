# SQLite to PostgreSQL 18 Migration Workflows & Pitfalls

This reference document tracks the workflow, setup commands, and technical pitfalls encountered when creating the PostgreSQL 18 backend and migrating the large SQLite karaoke library.

## 1. Gentoo PostgreSQL 18 Service Initialization

Gentoo Linux installs the binaries but does not initialize the PostgreSQL database cluster automatically. Attempting to start the service before initialization fails.

### Initialization & Startup Sequence:
1. **Initialize Cluster**: Configure path structures and defaults for PostgreSQL:
   ```bash
   sudo emerge --config dev-db/postgresql:18
   ```
2. **Start and Enable Service**:
   ```bash
   sudo systemctl start postgresql-18
   sudo systemctl enable postgresql-18
   ```
3. **Verify Connectivity**:
   ```bash
   pg_isready
   # Output: /run/postgresql:5432 - accepting connections
   ```

## 2. Role and Database Provisioning

By default, local connections use PostgreSQL's `trust` or `peer` authentication on Gentoo.

### Provisioning Commands:
```bash
# Create the user/role
sudo -u postgres psql -c "CREATE USER karaoke;"

# Create the database owned by the user (must run outside a transaction)
sudo -u postgres psql -c "CREATE DATABASE karaoke OWNER karaoke;"
```

## 3. SQLite to PostgreSQL Data Migration Pitfalls

When executing a one-time migration from SQLite to Postgres, three distinct database-level differences can cause migrations to fail.

### Pitfall A: Referential Integrity / Foreign Key Violations
SQLite does not enforce Foreign Key constraints by default unless `PRAGMA foreign_keys = ON;` is explicitly executed on every connection. This allows orphan rows (child rows pointing to non-existent parent IDs) to accumulate in tables like `sources`, `track_genre`, and `track_analysis`. Postgres enforces Foreign Keys strictly, leading to `ForeignKeyViolation` crashes.

*   **Solution**: Before executing batch inserts, query parent IDs (e.g. `track_id` in `tracks`, `recording_id` in `recordings`) and pre-filter child table rows to exclude any orphan records.

### Pitfall B: Embedded NUL (0x00) Characters in Strings
SQLite happily accepts embedded null bytes (`\x00`) in text columns, but PostgreSQL strictly forbids NUL characters inside text string literals, throwing `ValueError: A string literal cannot contain NUL (0x00) characters`.

*   **Solution**: Sanitize all string fields dynamically during row preparation by replacing `\x00` with an empty string `""`:
    ```python
    def clean_postgres_string(val):
        if isinstance(val, str):
            return val.replace("\x00", "")
        return val
    ```

### Pitfall C: SERIAL Primary Key Sequences Out-of-Sync
When copying explicit integer IDs from SQLite into Postgres columns mapped to `SERIAL` (e.g. `tracks(track_id)`), Postgres does not advance the underlying sequence generator. Future automatic inserts will attempt to use sequence IDs that already exist, causing `UniqueViolation` crashes.

*   **Solution**: Immediately after migrating each table carrying a `SERIAL` column, synchronize the sequence to match the maximum ID present:
    ```sql
    SELECT setval('table_column_seq', COALESCE((SELECT MAX(column) FROM table), 1), true);
    ```
