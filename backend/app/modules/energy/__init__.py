"""Energy / telemetry vertical slice.

Ingest from the simulator, keep a per-household live snapshot + ring buffer,
and expose dashboard-ready read APIs. No Postgres persistence in this module.
"""
