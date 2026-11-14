"""
Long-term memory (tracker 3.4): durable, user-visible facts about a household that help future answers.

    models       the `user_memory_items` table
    commands     "remember that ..." and "yaad rakhna ..." (deterministic, no model)
    repository   scoped queries: every read and write is bound to user AND household
    service      safety gate (mask, injection, length, cap, dedupe), CRUD, export, expiry sweep
    facts        the compact block the assistant may read, and the untrusted-tag wrapper
    schemas      HTTP payloads (the router lives in `agents_platform/api/memory.py`)

This is not conversation history (the checkpoint) and not telemetry. Nothing is saved from free chat: an item
exists only because the user said "remember ..." or used the API. The user can list, edit, delete and export it.
"""
