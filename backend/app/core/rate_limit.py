"""Shared slowapi Limiter instance, keyed by client IP."""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
