"""Energy / telemetry vertical slice.

HTTP ingest remains for tests/dev (`INGEST_HTTP_ENABLED`). Production ingest is
the RabbitMQ worker, which validates, writes TimescaleDB, updates Redis, then
ACKs. The dashboard connects to `GET /ws/energy`.
"""
