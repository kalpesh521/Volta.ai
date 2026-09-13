"""Origin allow-list for dashboard ports (including Cursor/WSL forwards)."""
from app.core.cors import is_allowed_origin
from app.core.config import settings


def test_localhost_dashboard_ports_allowed_in_development():
    assert settings.ENVIRONMENT.lower() != "production"
    assert is_allowed_origin(None) is True
    assert is_allowed_origin("http://127.0.0.1:8765") is True
    assert is_allowed_origin("http://127.0.0.1:8766") is True
    assert is_allowed_origin("http://localhost:8766") is True
    assert is_allowed_origin("https://evil.example") is False
