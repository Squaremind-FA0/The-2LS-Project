# PRP: G2 — Auth, roles, company tenancy

## Goal
Introduce companies, users with a role (`company_admin | dispatcher | worker`)
and worker profiles, with password login that issues a short-lived JWT access
token, a `GET /api/me` identity endpoint, and a company-admin-only endpoint that
creates (invites) a worker in the caller's company. After this PRP, every
`/api/*` route except an explicit allowlist is gated by one centralized
`require_role(...)` dependency, and every ORM query on tenant data is
structurally scoped to the caller's `company_id` (fail-closed), so a user of
company A can never read, list, or write company B's rows.

## Non-goals
- **Customer accounts.** v1 decision: jobs are entered by the company's
  dispatcher, not by customers. No customer role, signup, or portal.
- **SSO / OAuth / social login.** Email + password only.
- **Refresh tokens and a token denylist.** Deferred. Users re-login after
  `ACCESS_TOKEN_TTL_MIN`. Revocation is handled by `User.token_version` +
  `User.is_active` (see Data model), which needs no denylist table.
- **Self-service company signup / public registration.** Companies and their
  first `company_admin` are created via a seed/CLI path or test fixtures only
  (no public endpoint in this PRP).
- **Email delivery for invites.** The invite/create endpoint creates the user
  with an admin-supplied initial password; sending invite emails / set-password
  links is a later PRP.
- **Password reset, change-password endpoint, MFA, rate limiting / lockout.**
  `token_version` bump on password change is implemented in the model/service
  helper so a later endpoint gets revocation for free, but no endpoint is added.
- **Company slug at login / per-company email uniqueness.** v1 assumes email is
  globally unique (DB unique constraint on `users.email`).
- **Frontend login UI.** Backend only.
- **Dispatcher-specific endpoints.** The `dispatcher` role exists in the enum
  and is accepted by `require_role`, but no dispatcher-only route ships here.

## Context
Repo state (verified against `main` @ `52ec565`):

- `backend/app/config.py:5-17` — `Settings(BaseSettings)` with
  `DATABASE_URL` (default `sqlite:///./2ls.db`, line 8) and `JWT_SECRET: str`
  (line 9, required, no default). There is no `ACCESS_TOKEN_TTL_MIN` and no
  secret-length validator. The existing `@field_validator("CORS_ORIGINS",
  mode="before")` at lines 12-17 is the pattern to follow for the new
  `JWT_SECRET` validator. Module-level `settings = Settings()` at line 20.
- `backend/app/db.py:6-21` — `engine` (line 8, SQLite gets
  `check_same_thread=False` at line 6), `SessionLocal` (line 9),
  `Base(DeclarativeBase)` (lines 12-13), `get_db()` generator (lines 16-21).
  No tenant-scoped session variant exists yet.
- `backend/app/models/__init__.py:1-3` — only re-exports `Base`; no models.
  `backend/migrations/env.py:7,21` imports `Base` from `app.models` and uses
  `Base.metadata` as `target_metadata`, so new models **must** be imported in
  `app/models/__init__.py` for Alembic to see them.
- `backend/migrations/versions/0001_baseline.py:10-21` — revision `"0001"`,
  `down_revision = None`, empty `upgrade()`/`downgrade()`. The new migration is
  revision `"0002"`, `down_revision = "0001"`, following the same header style
  (typed `revision`/`down_revision`/`branch_labels`/`depends_on`).
- `backend/requirements.txt:6-7` — pins `pyjwt>=2.8` and `passlib[bcrypt]>=1.7`.
  Nothing in `backend/` imports passlib yet (`grep -r passlib backend/app` is
  empty), so swapping it is free.
- `backend/app/main.py:10-18` — `app = FastAPI(title="API")`, CORS middleware,
  and the only route `GET /api/health`. No `APIRouter` is wired yet; this PRP
  establishes the first router-wiring pattern (`app.include_router(...)`
  placed **before** the SPA catch-all block at lines 21-39 so `/api/*` routes
  match first).
- `backend/tests/conftest.py:3-4` — sets `JWT_SECRET="test-secret"` (11 bytes)
  and `DATABASE_URL="sqlite:///:memory:"` via `os.environ.setdefault` before
  importing the app. The 11-byte secret **will fail** the new ≥32-byte
  validator, so conftest must change in the same task as the validator.
  `client` fixture at lines 12-14 is plain `TestClient(app)`.
- `backend/tests/test_health.py:1-4` — existing health test; must keep passing
  and `/api/health` stays unauthenticated.
- `backend/tests/test_quality_tooling.py:4-12` — parses `requirements.txt`
  package names (strips `[extras]` and `>=`); requires `pytest` and `ruff`.
  The `pwdlib[argon2]>=0.2` line is compatible with that parser.
- `backend/pyproject.toml:1-6` — ruff `line-length = 100`, `target-version =
  "py312"`, rules `E,F,I` (import sorting enforced).
- Unmerged sibling branch `prp/G1` (commits `80838cd`, `9027e17`) adds
  `transactional_session()` to `app/db.py`, a `transactional_db` fixture to
  `conftest.py`, and makes the SPA catch-all `/{full_path:path}` in
  `app/main.py` unconditional. This PRP must be implemented **after G1 merges
  to `main`** (rebase `prp/G2` onto it) and should reuse G1's transactional
  fixture rather than inventing a second one.

### Design decisions (already reviewed — rationale, not open questions)
1. **Password hashing: `pwdlib[argon2]`, not passlib.** passlib is unmaintained
   since 2020, and the unbounded `passlib[bcrypt]>=1.7` pin breaks on current
   bcrypt (version-probe errors on bcrypt ≥ 4.1; startup self-test failure on
   bcrypt 5's 72-byte limit). Replace the line in `backend/requirements.txt`
   with `pwdlib[argon2]>=0.2` and use `PasswordHash.recommended()` (Argon2id)
   in a new `backend/app/security/passwords.py`. Argon2 has no 72-byte input
   truncation, which the 100+ byte password test locks in.
2. **JWT revocation without a denylist.** Token claims are only `sub` (user id
   as string), `tv` (int token_version), `iat`, `exp`. Never `role` or
   `company_id` in the token — they are loaded from the DB on every request in
   `get_current_user`, so a role change or deactivation takes effect
   immediately. Reject when the user is missing, `is_active` is False, or
   `tv != user.token_version`. `token_version` is bumped on deactivation, role
   change, and password change. Decode strictly:
   `jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"],
   options={"require": ["exp", "sub"]})`. New setting
   `ACCESS_TOKEN_TTL_MIN: int = 60` and a validator rejecting `JWT_SECRET`
   shorter than 32 bytes (UTF-8 encoded).
3. **Tenant scoping enforced structurally (fail-closed).** A `TenantScoped`
   mixin declares `company_id` (non-null FK → `companies.id`, indexed);
   `User`, `WorkerProfile`, and every future tenant table inherit it.
   `backend/app/tenancy.py` registers on `SessionLocal`:
   - a `do_orm_execute` listener that, for SELECT / UPDATE / DELETE ORM
     statements, adds `with_loader_criteria(TenantScoped, lambda cls:
     cls.company_id == cid, include_aliases=True)` with
     `cid = session.info["company_id"]`; if the key is absent and the statement
     does not carry `execution_options(skip_tenant_filter=True)`, it raises
     `TenantContextMissing` (fail closed). Statements that touch no
     `TenantScoped` entity (e.g. `Company` lookups) are not blocked.
   - a `before_flush` listener that, for each new `TenantScoped` object in
     `session.new`, stamps `company_id` from `session.info` if unset, and
     raises `TenantContextMissing` / `CrossTenantWrite` if there is no tenant
     context or the object's `company_id` differs from it.
   - `get_tenant_db(user = Depends(get_current_user))` yields a session with
     `db.info["company_id"] = user.company_id`. Routers depend on
     `get_tenant_db`, never on bare `get_db`.
   - **Single documented exception:** the pre-auth lookups (login by email in
     `POST /api/auth/login`, and user-by-id inside `get_current_user`) run on a
     bare session with `.execution_options(skip_tenant_filter=True)` set
     explicitly on that one statement. They live only in
     `backend/app/auth/service.py` so the bypass is greppable.
   - Cross-tenant reads return **404, not 403**, so existence of another
     company's resources is not disclosed (this falls out naturally: the
     scoped query returns `None` → 404).

## Data model / API contracts

### Settings (`backend/app/config.py`)
```python
class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./2ls.db"
    JWT_SECRET: str                  # required; validator: len(value.encode()) >= 32
    ACCESS_TOKEN_TTL_MIN: int = 60
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]
```
`JWT_SECRET` is supplied only via environment / untracked `.env`; it is never
committed. Tests set a ≥32-byte dummy value in `conftest.py` via
`os.environ.setdefault`.

### ORM models (`backend/app/models/`)
```python
# app/models/base.py  (or app/tenancy.py — mixin lives with the listeners)
class TenantScoped:
    @declared_attr
    def company_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"),
                             nullable=False, index=True)

class Role(str, enum.Enum):
    company_admin = "company_admin"
    dispatcher = "dispatcher"
    worker = "worker"

# app/models/company.py
class Company(Base):                       # NOT tenant-scoped (it IS the tenant)
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

# app/models/user.py
class User(TenantScoped, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lower-cased
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role",
                                            native_enum=False, length=20))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    worker_profile: Mapped["WorkerProfile | None"] = relationship(back_populates="user",
                                                                 uselist=False)

# app/models/worker_profile.py
class WorkerProfile(TenantScoped, Base):
    __tablename__ = "worker_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         unique=True)
    trades: Mapped[list[str]] = mapped_column(JSON, default=list)   # e.g. ["plumbing"]
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    active: Mapped[bool] = mapped_column(default=True, server_default=true())
    user: Mapped[User] = relationship(back_populates="worker_profile")
```
`JSON` (not a Postgres `ARRAY`) keeps the schema portable across SQLite (dev /
tests) and Postgres. `native_enum=False` avoids a Postgres `CREATE TYPE` that
would complicate `downgrade()`.

### Migration `backend/migrations/versions/0002_auth_tenancy.py`
```
revision = "0002"; down_revision = "0001"
upgrade():   create_table companies → users (FK companies.id, ix_users_company_id,
             uq/ix_users_email) → worker_profiles (FK companies.id, FK users.id unique,
             ix_worker_profiles_company_id)
downgrade(): drop_table worker_profiles → users → companies (reverse order, indexes
             dropped with their tables)
```

### Security helpers
```python
# app/security/passwords.py
def hash_password(plain: str) -> str: ...
def verify_password(plain: str, hashed: str) -> bool: ...   # never raises on mismatch

# app/security/tokens.py
def create_access_token(user_id: int, token_version: int,
                        *, now: datetime | None = None) -> str: ...
def decode_access_token(token: str) -> dict: ...   # raises InvalidTokenError (incl. expired)
```

### Dependencies (`backend/app/auth/deps.py`)
```python
bearer = HTTPBearer(auto_error=False)
def get_current_user(creds = Depends(bearer), db: Session = Depends(get_db)) -> User
    # 401 + WWW-Authenticate: Bearer on: missing/malformed header, bad signature,
    # expired, missing exp/sub, unknown user, is_active False, tv mismatch
def require_role(*roles: Role) -> Callable[..., User]
    # returns a dependency; 403 {"detail": "Insufficient role"} if user.role not in roles
def current_company_id(user: User = Depends(get_current_user)) -> int
def get_tenant_db(user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> Iterator[Session]
    # sets db.info["company_id"] = user.company_id
ALL_ROLES = (Role.company_admin, Role.dispatcher, Role.worker)
```
`require_role` marks its returned callable (e.g. attribute
`__require_role__ = roles`) so the route-audit test can detect it in a route's
dependency tree.

### Pydantic schemas (`backend/app/schemas/auth.py`, `backend/app/schemas/users.py`)
```python
class LoginRequest(BaseModel):
    email: EmailStr | str          # plain str + lower()/strip() validator if email-validator not added
    password: str = Field(min_length=1, max_length=1024)

class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int                 # seconds == ACCESS_TOKEN_TTL_MIN * 60

class WorkerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    trades: list[str]
    hourly_rate: Decimal
    active: bool

class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    full_name: str
    role: Role
    company_id: int
    company_name: str
    worker_profile: WorkerProfileOut | None

class WorkerCreateRequest(BaseModel):
    email: str = Field(max_length=320)            # normalized lower/strip
    full_name: str = Field(min_length=1, max_length=200)
    initial_password: str = Field(min_length=12, max_length=1024)
    trades: list[str] = Field(default_factory=list, max_length=20)
    hourly_rate: Decimal = Field(ge=0, max_digits=10, decimal_places=2)

class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int                         # user id
    email: str
    full_name: str
    role: Literal["worker"]
    company_id: int
    is_active: bool
    worker_profile: WorkerProfileOut
```
Note: `company_id` is **never** accepted in any request body — it always comes
from `get_tenant_db` / `current_company_id`.

### Endpoints
```
POST /api/auth/login                         (public — allowlisted)
  request:  LoginRequest  { email: str, password: str }
  200:      TokenResponse { access_token: str, token_type: "bearer", expires_in: 3600 }
  401:      { detail: "Invalid credentials" }   # same body for unknown email,
                                                # wrong password, inactive user
  422:      validation error

GET /api/me                                  require_role(*ALL_ROLES)
  headers:  Authorization: Bearer <token>
  200:      MeResponse
  401:      missing / invalid / expired / revoked token

POST /api/workers                            require_role(Role.company_admin)
  request:  WorkerCreateRequest
  201:      WorkerOut   (User role=worker + WorkerProfile, company_id = caller's)
  401:      unauthenticated
  403:      { detail: "Insufficient role" }    # dispatcher or worker caller
  409:      { detail: "Email already registered" }
  422:      validation error

GET /api/workers                             require_role(company_admin, dispatcher)
  200:      list[WorkerOut]  — only the caller's company

GET /api/workers/{user_id}                   require_role(company_admin, dispatcher)
  200:      WorkerOut
  404:      { detail: "Worker not found" }     # nonexistent OR in another company
```
`GET /api/workers` and `GET /api/workers/{user_id}` are included because they
are the minimum read surface needed to prove the cross-tenant 404 and
list-isolation acceptance criteria.

### Allowlist of unauthenticated `/api/*` routes
`{"/api/health", "/api/auth/login"}` — defined once as
`PUBLIC_API_PATHS` in `backend/app/auth/deps.py` and consumed by the
route-audit test.

## Task list
TDD-ordered: each `(test)` task is committed RED (observe the failure) before
the `(impl)` task that turns it GREEN. Run the validation gates after every
task. Prerequisite: `prp/G1` merged to `main` and `prp/G2` rebased onto it.

1. **(test)** `backend/tests/test_passwords.py` — failing tests for
   `app.security.passwords`: hash/verify round-trip returns True; wrong
   password returns False; a 100+ byte password (e.g. `"é" * 60 + "x" * 30`)
   round-trips **and** a variant differing only after byte 72 fails verify
   (proves no bcrypt-style truncation); hash string starts with `$argon2id$`.
2. **(impl)** `backend/requirements.txt` — replace `passlib[bcrypt]>=1.7`
   with `pwdlib[argon2]>=0.2`; `backend/app/security/__init__.py`,
   `backend/app/security/passwords.py` — `_hasher = PasswordHash.recommended()`,
   `hash_password`, `verify_password`.
3. **(test)** `backend/tests/test_config.py` — failing tests: `Settings(JWT_SECRET="x"*31)`
   raises `ValidationError`; `Settings(JWT_SECRET="x"*32)` succeeds;
   `ACCESS_TOKEN_TTL_MIN` defaults to 60 and is overridable via env.
4. **(impl)** `backend/app/config.py` — add `ACCESS_TOKEN_TTL_MIN: int = 60` and
   `@field_validator("JWT_SECRET")` enforcing ≥32 UTF-8 bytes;
   `backend/tests/conftest.py` — change the `JWT_SECRET` default to a ≥32-byte
   non-secret test string (e.g. `"test-only-jwt-secret-not-for-production-use"`).
5. **(test)** `backend/tests/test_tokens.py` — failing tests for
   `app.security.tokens`: created token decodes to `sub == str(user_id)`,
   `tv`, `iat`, `exp`, and contains **no** `role`/`company_id` keys; a token
   built with `now = utcnow() - TTL - 1s` raises `jwt.ExpiredSignatureError`;
   a token signed with another secret raises `InvalidSignatureError`; a token
   with `alg=none` is rejected; a token missing `exp` is rejected.
6. **(impl)** `backend/app/security/tokens.py` — `create_access_token`,
   `decode_access_token` using `jwt.encode(..., algorithm="HS256")` and
   `jwt.decode(..., algorithms=["HS256"], options={"require": ["exp", "sub"]})`.
7. **(test)** `backend/tests/test_models.py` — failing tests (using G1's
   `transactional_db` fixture with `Base.metadata.create_all`): `Company`,
   `User`, `WorkerProfile` persist; `User`/`WorkerProfile` inherit
   `TenantScoped` and `company_id` is non-null + indexed; `users.email` unique
   violation raises `IntegrityError`; `token_version` defaults 0, `is_active`
   defaults True; `WorkerProfile.trades` round-trips a list.
8. **(impl)** `backend/app/models/tenant.py` (`TenantScoped` mixin, `Role`
   enum), `backend/app/models/company.py`, `backend/app/models/user.py`,
   `backend/app/models/worker_profile.py`; `backend/app/models/__init__.py` —
   export `Base, Company, User, WorkerProfile, TenantScoped, Role` so
   `migrations/env.py:7` registers them. Add `User.bump_token_version()` and
   service helpers `deactivate_user`, `set_role`, `set_password` that bump it.
9. **(test)** `backend/tests/test_migrations.py` — failing test that runs
   `alembic upgrade head` against a temp-file SQLite URL, asserts tables
   `companies`, `users`, `worker_profiles` and indexes `ix_users_company_id`,
   `ix_worker_profiles_company_id` exist, then `alembic downgrade base` and
   asserts those tables are gone (reversibility).
10. **(impl)** `backend/migrations/versions/0002_auth_tenancy.py` — revision
    `"0002"`, `down_revision = "0001"`, `upgrade()` creating the three tables
    and indexes per the Data model, `downgrade()` dropping them in reverse
    order. Hand-written (not blind autogenerate); diff against
    `alembic revision --autogenerate` output locally to confirm no drift, then
    discard the autogen file.
11. **(test)** `backend/tests/test_tenancy.py` — failing tests for the session
    listeners: a SELECT of `User` on a session with no `info["company_id"]`
    raises `TenantContextMissing`; the same SELECT with
    `execution_options(skip_tenant_filter=True)` succeeds; with
    `info["company_id"] = A`, `select(User)` returns only company A users,
    `session.get(User, b_user_id)` returns `None`, and a joined/aliased query
    through `aliased(User)` is also filtered; bulk `update(User)` /
    `delete(WorkerProfile)` only affect company A rows; `before_flush` stamps
    `company_id` on a new `WorkerProfile` with none set, and raises when adding
    an object whose `company_id` is B; `select(Company)` with no tenant
    context is not blocked.
12. **(impl)** `backend/app/tenancy.py` — `TenantContextMissing`,
    `CrossTenantWrite`, `do_orm_execute` listener using
    `with_loader_criteria(TenantScoped, lambda cls: cls.company_id == cid,
    include_aliases=True)`, `before_flush` listener; register both on
    `SessionLocal` (import side effect from `app/db.py` or explicit
    `install_tenant_listeners(SessionLocal)` called in `app/db.py`).
13. **(test)** `backend/tests/conftest.py` + `backend/tests/factories.py` —
    add the **two-company isolation fixture** `two_companies`: company A and
    company B, each with one `company_admin` and one `worker` (with
    `WorkerProfile`), known passwords, created inside the transactional DB;
    plus `auth_header(user)` helper and a `client` override wiring
    `app.dependency_overrides[get_db]` to the transactional session (use a
    `StaticPool` engine for `sqlite:///:memory:` so the app and test share one
    connection). Then `backend/tests/test_auth_login.py` — failing tests:
    - correct email/password → 200 `TokenResponse`, `expires_in == 3600`;
    - **login with a bad password → 401** `{"detail": "Invalid credentials"}`;
    - unknown email → 401 with the identical body;
    - inactive user → 401;
    - email is matched case-insensitively.
14. **(impl)** `backend/app/auth/__init__.py`, `backend/app/auth/service.py`
    (`authenticate(db, email, password)` — the **only** place using
    `skip_tenant_filter=True`, with a comment explaining why;
    run `verify_password` against a dummy hash when the email is unknown to
    reduce timing-based user enumeration), `backend/app/schemas/__init__.py`,
    `backend/app/schemas/auth.py`, `backend/app/routers/__init__.py`,
    `backend/app/routers/auth.py` (`APIRouter(prefix="/api/auth",
    tags=["auth"])`, `POST /login`), and `backend/app/main.py` —
    `app.include_router(auth.router)` placed before the SPA catch-all.
15. **(test)** `backend/tests/test_me.py` — failing tests:
    - valid token → 200 `MeResponse` with role, `company_id`, `company_name`,
      `worker_profile` populated for a worker and `null` for an admin;
    - no `Authorization` header → 401;
    - **request with an expired JWT → 401** (token minted via
      `create_access_token(..., now=utcnow() - timedelta(minutes=TTL+1))`);
    - token signed with a wrong secret → 401;
    - token whose `tv` is stale (after `deactivate_user` / `set_role` /
      `set_password` bumps `token_version`) → 401;
    - token for a user with `is_active=False` → 401;
    - DB role change takes effect without re-login (role read from DB, not
      token): demote admin → worker, same token now gets 401 (tv bumped).
16. **(impl)** `backend/app/auth/deps.py` — `get_current_user`, `require_role`,
    `ALL_ROLES`, `current_company_id`, `get_tenant_db`, `PUBLIC_API_PATHS`
    (user-by-id lookup in `get_current_user` goes through
    `auth/service.py:get_user_for_token`, the second documented
    `skip_tenant_filter` use); `backend/app/schemas/users.py` (`MeResponse`,
    `WorkerProfileOut`); `backend/app/routers/me.py` (`GET /api/me`,
    `dependencies=[Depends(require_role(*ALL_ROLES))]`, reads via
    `get_tenant_db`); wire in `backend/app/main.py`.
17. **(test)** `backend/tests/test_workers.py` — failing tests:
    - company_admin `POST /api/workers` → 201 `WorkerOut`, `company_id` equals
      the admin's company even if the body smuggles a `company_id` field
      (extra field ignored/rejected), and the new worker can log in;
    - **worker role calling `POST /api/workers` → 403**
      `{"detail": "Insufficient role"}`;
    - dispatcher calling `POST /api/workers` → 403;
    - duplicate email (including one that exists in company B) → 409;
    - `initial_password` shorter than 12 chars → 422;
    - unauthenticated → 401.
18. **(impl)** `backend/app/schemas/users.py` (`WorkerCreateRequest`,
    `WorkerOut`), `backend/app/services/workers.py`
    (`create_worker(db, payload) -> User` — uses `get_tenant_db` session,
    relies on `before_flush` to stamp `company_id`), `backend/app/routers/workers.py`
    (`POST /api/workers` with `require_role(Role.company_admin)`;
    `GET /api/workers` and `GET /api/workers/{user_id}` with
    `require_role(Role.company_admin, Role.dispatcher)`); wire in `main.py`.
19. **(test)** `backend/tests/test_tenant_isolation.py` — failing tests using
    the `two_companies` fixture:
    - **user of company A reading company B's worker
      (`GET /api/workers/{b_worker_id}` with A-admin token) → 404**, body
      `{"detail": "Worker not found"}` — identical to a truly nonexistent id;
    - A-admin `GET /api/workers` lists only A's worker(s), never B's;
    - A-admin `POST /api/workers` creates the worker in company A only;
      B-admin's list is unchanged;
    - `GET /api/me` with A's token never exposes company B data;
    - symmetric checks from company B's side.
20. **(impl)** fix-ups in `backend/app/routers/workers.py` /
    `backend/app/services/workers.py` only if (19) exposes a gap (expected:
    already GREEN from the tenancy listener — if so, commit the test alone and
    note it in the commit body).
21. **(test)** `backend/tests/test_route_audit.py` — **route-audit test**: walk
    `app.routes`, and for every `APIRoute` whose `path` starts with `/api/`,
    recursively walk `route.dependant.dependencies` and assert a dependency
    carrying the `require_role` marker is present — unless `path` is in
    `PUBLIC_API_PATHS` (`/api/auth/login`, `/api/health`). Also assert that
    no `/api/*` route other than the allowlist depends directly on bare
    `get_db` (they must use `get_tenant_db`), and that the allowlist itself
    contains no unexpected entries. Include a self-check: a throwaway
    `FastAPI()` with an unguarded `/api/leak` route makes the audit helper
    report a violation (so the audit can't silently pass on an empty walk).
22. **(impl)** any missing `require_role` wiring surfaced by (21);
    `backend/app/main.py` — keep `include_router` calls grouped in one block
    with a comment pointing at `test_route_audit.py` as the enforcement.
23. **(docs)** `CLAUDE.md` — under "Run", note that `JWT_SECRET` (≥32 bytes)
    must be set in the environment or an untracked `backend/.env`; add
    `cd backend && alembic upgrade head` / `alembic downgrade base` to the
    "Test / validate" block (per `CLAUDE.md:34-36`, new gates are recorded
    there). No secret value is written anywhere.

## Validation gates
```bash
cd backend && pytest -q
cd backend && ruff check .
cd backend && alembic upgrade head
cd backend && alembic downgrade base
```
The alembic gates need `JWT_SECRET` (≥32 bytes) in the environment because
`migrations/env.py:6` imports `app.config.settings`; use a throwaway local
value, e.g. `JWT_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')`,
and a scratch `DATABASE_URL=sqlite:///./g2-gate.db` (`*.db` is gitignored).
Run `upgrade head` again after `downgrade base` to confirm the round trip.

## Acceptance criteria
- `backend/requirements.txt` no longer lists `passlib`; it lists
  `pwdlib[argon2]`. `hash_password` produces `$argon2id$...` hashes and a
  100+ byte password round-trips without truncation.
- `Settings` rejects a `JWT_SECRET` under 32 bytes and exposes
  `ACCESS_TOKEN_TTL_MIN` (default 60). No secret value is committed.
- `alembic upgrade head` creates `companies`, `users`, `worker_profiles`;
  `alembic downgrade base` removes them; revision `0002` has
  `down_revision = "0001"`.
- `POST /api/auth/login` returns `TokenResponse` on valid credentials and
  **401 on a bad password** (same body for unknown email / inactive user).
- Decoded access tokens contain only `sub`, `tv`, `iat`, `exp`.
- `GET /api/me` returns `MeResponse`; **an expired JWT → 401**; a stale
  `token_version` or inactive user → 401.
- `POST /api/workers` by a `company_admin` returns 201 `WorkerOut` in the
  admin's company; **a `worker` caller → 403**; a `dispatcher` caller → 403.
- **Company A's admin reading company B's worker → 404** (not 403); list and
  create are isolated per company — proven by `test_tenant_isolation.py`
  with the two-company fixture.
- `test_route_audit.py` passes: every `/api/*` route except
  `/api/auth/login` and `/api/health` depends on `require_role`, and none use
  bare `get_db`.
- A session without `info["company_id"]` querying a `TenantScoped` model
  raises `TenantContextMissing`; `skip_tenant_filter=True` appears only in
  `backend/app/auth/service.py` (`grep -rn skip_tenant_filter backend/app`).
- `GET /api/health` still returns `{"status": "ok"}` unauthenticated.
- All four validation gates pass.

## Risks
- **`sqlite:///:memory:` is per-connection.** `conftest.py:4` uses an
  in-memory URL; without `poolclass=StaticPool` (and the G1 transactional
  connection) the app's request session and the test's session see
  different empty databases → "no such table". Build the test engine with
  `StaticPool` and override `get_db`.
- **conftest `JWT_SECRET` too short.** `conftest.py:3` sets an 11-byte value;
  the new validator fails at import of `app.config`, breaking *every* test.
  Task 4 changes both in the same commit.
- **G1 not yet on `main`.** G1 changes `app/db.py`, `conftest.py`, and
  `app/main.py` (unconditional SPA catch-all `/{full_path:path}`). Implementing
  G2 before G1 merges will conflict on exactly those files. Routers must be
  included **before** the catch-all, or `/api/*` POSTs could hit it (GET only
  today, but ordering is the safe invariant). The route audit filters to
  `APIRoute` with `/api/` prefix, so the catch-all is not flagged.
- **Fail-closed listener breaking non-request code.** Alembic migrations,
  seed scripts, and test fixtures that query `TenantScoped` models without
  tenant context will raise `TenantContextMissing`. That is intended; fixtures
  must set `session.info["company_id"]` or use `skip_tenant_filter=True`
  explicitly (test code only — production code exception stays in
  `auth/service.py`). Migrations use `op.*` Core operations, which do not go
  through ORM `do_orm_execute`.
- **`with_loader_criteria` coverage gaps.** It applies to ORM `select`,
  `session.get`, relationship lazy/eager loads, and ORM-enabled
  `update`/`delete`, but **not** to raw `text()` SQL or Core
  `connection.execute(table.select())`. Rule for this and future PRPs: no raw
  SQL against tenant tables in request code. `session.get()` may return an
  identity-map hit without re-querying — tests in task 11 must use a fresh
  session or `populate_existing` to prove the 404.
- **Lambda caching in `with_loader_criteria`.** The lambda must close over
  `cid` per execution (pass `track_closure_variables=True`, the default) or
  SQLAlchemy's statement cache could reuse company A's criteria for B. Task
  11's A-then-B test in the same process catches this.
- **Global email uniqueness.** Two companies cannot employ the same email
  address. Accepted v1 trade-off (no company slug at login); revisit if
  contractors work for multiple companies. Also: duplicate-email 409 reveals
  that the email exists in *some* company — acceptable because only
  company_admins can call it.
- **No refresh token / denylist.** A stolen token is valid until `exp`
  (≤60 min) unless `token_version` is bumped. Keep TTL short; refresh
  tokens are an explicit follow-up.
- **Login timing enumeration.** Unknown-email path must still run an Argon2
  verify against a dummy hash; otherwise response time reveals registered
  emails.
- **Argon2 cost in tests.** `PasswordHash.recommended()` is deliberately slow
  (~tens of ms). The `two_companies` fixture hashes 4 passwords per test;
  if the suite becomes slow, precompute hashes once per session in the
  fixture rather than lowering production parameters.
- **`Numeric` on SQLite** emits a Decimal warning and stores as float-ish
  text; compare `hourly_rate` as `Decimal` with 2dp in tests.

## Rollback
- **Before merge:** delete the branch — `git branch -D prp/G2` (and
  `git push origin --delete prp/G2` if pushed). `main` is untouched.
- **After merge, before any deploy has run the migration:**
  `git revert -m 1 <G2 merge commit>` on `main` (or revert the squash commit).
- **After the migration has run on a database:**
  `cd backend && alembic downgrade 0001` (drops `worker_profiles`, `users`,
  `companies` — **destroys all users/companies data**; take a DB backup
  first), then deploy the reverted code.
- **Dependency:** the revert restores `passlib[bcrypt]>=1.7` in
  `backend/requirements.txt`; since no code imported passlib before G2,
  nothing else needs to change.
- **Config:** `JWT_SECRET` env values in deployed environments may stay set
  (harmless) or be removed; `ACCESS_TOKEN_TTL_MIN` disappears with the revert.
