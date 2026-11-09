"""A token must survive small clock differences between the server that issued it and the one checking it."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import settings
from app.core.security import TokenType, decode_token


def _token(*, issued: timedelta = timedelta(0), lifetime: timedelta = timedelta(minutes=15)) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": "u", "type": TokenType.ACCESS, "iat": now + issued, "exp": now + issued + lifetime},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def test_a_token_issued_slightly_in_the_future_is_accepted():
    """The validating clock is a second or two behind: seen on WSL2, and between any two servers."""
    assert decode_token(_token(issued=timedelta(seconds=3)))["sub"] == "u"


def test_a_token_that_just_expired_is_still_accepted_within_the_leeway():
    assert decode_token(_token(issued=-timedelta(minutes=15), lifetime=timedelta(minutes=15, seconds=-3)))["sub"] == "u"


@pytest.mark.parametrize("seconds", [settings.JWT_LEEWAY_SECONDS + 30, 3600])
def test_a_token_from_far_in_the_future_is_rejected(seconds):
    with pytest.raises(jwt.ImmatureSignatureError):
        decode_token(_token(issued=timedelta(seconds=seconds)))


def test_a_token_expired_beyond_the_leeway_is_rejected():
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(_token(issued=-timedelta(hours=1), lifetime=timedelta(minutes=15)))
