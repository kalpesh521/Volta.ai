# Volta Auth Service

Production-style authentication API built with FastAPI, async SQLAlchemy 2.0,
PostgreSQL, and JWT-based auth (password + Google OAuth2/OIDC).

## Stack

- **Python 3.12**, **FastAPI**
- **PostgreSQL** + **SQLAlchemy 2.0 (async)** + **Alembic** migrations
- **Pydantic v2** / **pydantic-settings**
- **PyJWT** (HS256) for access/refresh tokens, **passlib[bcrypt]** for password hashing
- **httpx** + **PyJWT's JWKS client** for Google OAuth2 / OIDC verification
- **slowapi** for rate limiting
- **pytest** + **httpx.AsyncClient** for integration tests (SQLite in-memory)

## Project layout

```
backend/
  main.py                 ASGI re-export (`uvicorn main:app`)
  app/
    main.py               FastAPI app: CORS, rate limit, routers
    core/                 shared infra (config, db, security, get_current_user)
    models/               shared ORM tables + Alembic barrel (User, tokens, …)
    modules/
      auth/               vertical slice: router, service, repos, oauth, schemas
      onboarding/         vertical slice: router, service, repo, models, schemas
  tests/                  pytest suite (outside the app package)
  alembic/                migrations
```

Request flow (inside each module): `router` → `service` (business logic, e.g.
"don't leak whether an email exists") → `repository` (DB reads/writes) →
`model`. Shared authn is `get_current_user` in `app/core/deps.py`. Auth-specific
wiring is `app/modules/auth/deps.py`.

## 1. Database: Neon (cloud, current) or local PostgreSQL (later)

The app reads its DB connection from a single `DATABASE_URL` + `DB_SSL_REQUIRED`
pair in `.env` - nothing else in the code changes when you switch between a
hosted DB (Neon) and a local Postgres instance.

### Option A: Neon (what you're using now)

1. Go to [neon.tech](https://neon.tech), sign up, create a project (e.g. `volta-auth`).
2. On the project dashboard, copy the connection string. Neon gives you
   something like:
   ```
   postgresql://neondb_owner:AbCdEf123@ep-cool-name-12345.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
3. Translate it into `.env` as **two separate fields** (see step 2 below for
   the full `.env` setup):
   - `DATABASE_URL` = the same string, but:
     - scheme changed from `postgresql://` to `postgresql+asyncpg://`
     - the `?sslmode=require` suffix **removed** (asyncpg doesn't parse it the
       way psycopg2 does - see the comment in `.env.example`)
   - `DB_SSL_REQUIRED=true`

   Example:
   ```env
   DATABASE_URL=postgresql+asyncpg://neondb_owner:AbCdEf123@ep-cool-name-12345.us-east-2.aws.neon.tech/neondb
   DB_SSL_REQUIRED=true
   ```
4. Skip straight to step 4 ("Run the database migration") below - no local
   Postgres install, no pgAdmin, no service to start. You can browse/query
   your tables in Neon's own browser SQL editor.

### Option B: Local PostgreSQL (when you're ready to switch)

Only two things change - the rest of the app is untouched:

```env
DATABASE_URL=postgresql+asyncpg://volta_user:volta_password@localhost:5432/volta_auth
DB_SSL_REQUIRED=false
```

Then run `alembic upgrade head` again to create the schema on the new
(empty) database - migrations recreate structure, not data. If you also need
the actual rows, `pg_dump`/`pg_restore` from Neon to local separately; that's
a data-migration step, not a code change.

The rest of this section walks through installing local Postgres, in case
you set it up later:

You already have PostgreSQL 14 installed but it isn't running. From your own
terminal (not this sandbox - it needs real `sudo`):

```bash
# Start the PostgreSQL service
sudo service postgresql start
# or, if using systemd:
sudo systemctl start postgresql

# Confirm it's up
pg_isready
```

Create the app's database and a dedicated user (avoid using the `postgres`
superuser for the app):

```bash
sudo -u postgres psql -c "CREATE USER volta_user WITH PASSWORD 'volta_password';"
sudo -u postgres psql -c "CREATE DATABASE volta_auth OWNER volta_user;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE volta_auth TO volta_user;"
```

Verify you can connect:

```bash
psql "postgresql://volta_user:volta_password@localhost:5432/volta_auth" -c "\conninfo"
```

(Optional) create a second database for running tests against real Postgres
instead of the default in-memory SQLite test setup:

```bash
sudo -u postgres psql -c "CREATE DATABASE volta_auth_test OWNER volta_user;"
```

## 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:
- `DATABASE_URL` - matches what you created above by default.
- `JWT_SECRET_KEY` - generate a real one:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(64))"
  ```
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` - from
  [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
  (create an OAuth 2.0 Client ID, add `http://localhost:8000/auth/google/callback`
  as an authorized redirect URI). You can leave these as placeholders if you're
  not testing the Google login flow locally.
- `CORS_ORIGINS` - list of your frontend origin(s).

## 3. Install dependencies (you already have a `venv` on Python 3.12)

```bash
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Run the database migration

```bash
alembic upgrade head
```

This creates `users`, `refresh_tokens`, `auth_providers`, and
`password_reset_tokens`.

To generate a new migration after changing a model:

```bash
alembic revision --autogenerate -m "describe your change"
alembic upgrade head
```

## 5. Run the API

```bash
uvicorn main:app --reload
```

Visit `http://localhost:8000/docs` for interactive Swagger UI, or
`http://localhost:8000/health` for a liveness check.

## 6. Run the tests

```bash
pytest -v
```

The suite runs against an in-memory SQLite database (via `aiosqlite`) so it
needs no live Postgres and each test is isolated in its own rolled-back
transaction. All models use a portable `GUID` column type
(`app/core/types.py`) specifically so the same models work identically on
Postgres in production and SQLite in tests. Google OAuth tests use a fake
`OAuthProviderClient` injected via `dependency_overrides` - no network calls.

## API summary

| Method | Path                          | Auth        | Notes |
|--------|-------------------------------|-------------|-------|
| POST   | `/auth/signup`                | -           | 201 + user (`name`, email; no password) or 409 if email taken |
| POST   | `/auth/login`                 | -           | rate-limited; generic 401 on any failure |
| POST   | `/auth/refresh`               | -           | rotates refresh token |
| POST   | `/auth/logout`                | -           | revokes a refresh token |
| GET    | `/auth/me`                    | Bearer JWT  | current user |
| POST   | `/auth/password-reset/request`| -           | rate-limited; always 204 |
| POST   | `/auth/password-reset/confirm`| -           | consumes single-use token |
| GET    | `/auth/google/login`          | -           | redirects to Google consent screen |
| GET    | `/auth/google/callback`       | -           | exchanges code, logs in/links/creates |
| POST   | `/auth/google/link-confirm`   | -           | completes linking an unverified-email Google identity |
| POST   | `/energy/ingest`              | X-Ingest-Token | simulator tick; 202 + normalized record |
| GET    | `/energy/{id}/live`           | Bearer JWT  | dashboard snapshot (solar, load, battery, grid, devices, weather) |
| GET    | `/energy/{id}/battery`        | Bearer JWT  | latest battery status |
| GET    | `/energy/{id}/grid`           | Bearer JWT  | latest grid status |
| GET    | `/energy/{id}/devices`        | Bearer JWT  | latest appliance readings |
| GET    | `/energy/{id}/weather`        | Bearer JWT  | weather used for the latest tick |
| GET    | `/energy/{id}/history`        | Bearer JWT  | recent ticks from the in-memory ring buffer |
| GET    | `/energy/{id}/daily`          | Bearer JWT  | daily kWh totals (`?date=YYYY-MM-DD`) |
| GET    | `/energy/{id}/hourly`         | Bearer JWT  | 24 hourly kWh buckets for one date |

Ingest is **not** a user JWT — the simulator sends `X-Ingest-Token` matching `INGEST_TOKEN` in `.env`. Live/summary routes reuse `get_current_user`. State is in-memory (process restart clears it). Point the simulator at the API with:

```bash
python -m simulator.main --ingest-url http://127.0.0.1:8000 --ingest-token dev-ingest-token --weather-mode fallback --ticks 5 --speed 0
```

All errors use the shape:

```json
{"success": false, "error": {"code": "invalid_credentials", "message": "Incorrect email or password"}}
```

## Security trade-offs (please review)

- **HS256 vs RS256 for JWT**: chose **HS256** (single shared secret) since
  this service is both the issuer and the only verifier of its own tokens -
  there's no third-party service that needs to verify tokens with a public
  key. RS256 would only pay off once other services need to verify tokens
  without holding the signing secret. Switching later just means changing
  `JWT_ALGORITHM` and using a key pair instead of `JWT_SECRET_KEY`.
- **`python-jose` vs `PyJWT`**: chose **PyJWT** - `python-jose` has had
  unpatched CVEs and is effectively unmaintained; PyJWT is the actively
  maintained standard and also gives us `PyJWKClient` for verifying Google's
  RS256-signed ID tokens against their published JWKS.
- **Refresh token reuse detection**: if a refresh token is used after it's
  already been rotated/revoked, we revoke *all* active refresh tokens for
  that user (not just the one reused). This is a deliberate "assume breach"
  stance - a small UX cost (forces re-login everywhere) for a real security
  win if a token was stolen.
- **OAuth email-verification gate**: Google's `email_verified` claim is
  trusted for auto-linking, but if false, we never silently attach a Google
  identity to an existing password account - we require the user to prove
  they hold the existing password first (`/auth/google/link-confirm`). This
  avoids a known account-takeover vector (attacker registers an
  unverified-email Google account matching a victim's email).
- **State cookie for CSRF, not server-side session storage**: the OAuth
  `state` parameter is stored in a short-lived `httponly` cookie rather than
  a server-side store, so the auth service can stay stateless (no session
  table) while still preventing CSRF on the callback. Trade-off: if a user
  starts the flow on one device/browser and completes it on another, it
  will fail closed (safe default, not a security compromise).
- **SQLite for tests vs a real Postgres test DB**: models use a portable
  `GUID` type so the full test suite runs fast against in-memory SQLite with
  zero external dependencies. This is a deliberate deviation from "always
  test against the production DB engine" for velocity; if you want to also
  run the suite against real Postgres, point `DATABASE_URL`/`TEST_DATABASE_URL`
  at `volta_auth_test` and adapt `conftest.py`'s engine - the SQL used here
  (via the ORM) doesn't rely on any Postgres-only feature, so this is a
  reasonably safe trade-off for this schema.
- **Rate limiting is in-memory (slowapi default)**: fine for a single
  process, but won't share state across multiple app instances behind a load
  balancer. For multi-instance deployments, back slowapi with Redis.

## Not implemented (explicitly out of scope per request)

- Email verification on signup (stub only: the reset-password "email" is
  simulated via a `print()` in `AuthService.request_password_reset` - swap
  in a real mail provider there).
- Docker/docker-compose (explicitly skipped per request).
