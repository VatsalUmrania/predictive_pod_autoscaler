# nexus/deploy/db-proxy/
# ========================
# Tier 1 — Zero-Code DB Proxy (PgBouncer)
#
# Sits between your application and PostgreSQL.
# Your app points DATABASE_URL at PgBouncer (:5432) instead of Postgres directly.
# PgBouncer pools connections AND we run a lightweight query interceptor sidecar
# (nexus-db-proxy) that tails the PgBouncer log and emits slow query / spike events
# to the NEXUS Status API via /sdk/query.
#
# Usage — add to your existing docker-compose.yaml:
#
#   include:
#     - path: ../../deploy/nexus/db-proxy/docker-compose.pgbouncer.yaml
#       env_file: .env
#
# Then change your DATABASE_URL from:
#   postgresql://user:pass@postgres:5432/mydb
# to:
#   postgresql://user:pass@pgbouncer:5432/mydb
#
# That's it. Your app code doesn't change at all.
