"""Route audit: every /api/* route is role-guarded and tenant-scoped.

Fails the build if someone adds an /api route without require_role(...),
reads through bare get_db instead of get_tenant_db, or grows the public
allowlist.
"""

from dataclasses import dataclass

from fastapi import APIRouter, Depends, FastAPI, routing
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.auth.deps import (
    ALL_ROLES,
    PUBLIC_API_PATHS,
    REQUIRE_ROLE_MARKER,
    get_tenant_db,
    require_role,
)
from app.db import get_db
from app.main import app

EXPECTED_PUBLIC = {"/api/health", "/api/auth/login"}


@dataclass(frozen=True)
class EffectiveRoute:
    path: str
    original: object
    methods: frozenset[str]
    dependant: Dependant | None


def effective_routes(application: FastAPI) -> list[EffectiveRoute]:
    """Every route as the app will actually serve it.

    FastAPI >= 0.16x keeps included routers as lazy ``_IncludedRouter``
    entries in ``app.routes`` (no path, no dependant), so a naive walk of
    ``app.routes`` silently skips them. ``iter_route_contexts`` (used by
    FastAPI's own OpenAPI generation) resolves them with prefixes and
    include-time dependencies applied. Older FastAPI flattens routes itself.
    """
    if hasattr(routing, "iter_route_contexts"):
        contexts = routing.iter_route_contexts(application.routes)
        return [
            EffectiveRoute(
                path=ctx.path or "",
                original=ctx.original_route,
                methods=frozenset(ctx.methods or ()),
                dependant=getattr(ctx, "dependant", None),
            )
            for ctx in contexts
        ]
    return [
        EffectiveRoute(
            path=getattr(r, "path", ""),
            original=r,
            methods=frozenset(getattr(r, "methods", None) or ()),
            dependant=getattr(r, "dependant", None),
        )
        for r in application.routes
    ]


def _walk(dependant: Dependant):
    for dep in dependant.dependencies:
        yield dep
        yield from _walk(dep)


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def api_routes(application: FastAPI) -> list[EffectiveRoute]:
    return [r for r in effective_routes(application) if _is_api(r.path)]


def audit_routes(application: FastAPI, public: frozenset[str] = PUBLIC_API_PATHS) -> list[str]:
    violations = []
    for route in api_routes(application):
        if not isinstance(route.original, APIRoute) or route.dependant is None:
            # A Mount / sub-app under /api would bypass this whole audit.
            kind = type(route.original).__name__
            violations.append(f"{route.path}: non-APIRoute under /api ({kind})")
            continue
        if route.path in public:
            continue
        label = f"{','.join(sorted(route.methods))} {route.path}"
        if not any(hasattr(dep.call, REQUIRE_ROLE_MARKER) for dep in _walk(route.dependant)):
            violations.append(f"{label}: missing require_role(...)")
        if any(dep.call is get_db for dep in route.dependant.dependencies):
            violations.append(f"{label}: depends on bare get_db (use get_tenant_db)")
    return violations


def test_every_api_route_is_guarded_and_tenant_scoped():
    assert audit_routes(app) == []


def test_public_allowlist_is_exactly_health_and_login():
    assert set(PUBLIC_API_PATHS) == EXPECTED_PUBLIC
    api_paths = {r.path for r in api_routes(app)}
    assert EXPECTED_PUBLIC <= api_paths  # no stale allowlist entries


def test_audit_actually_sees_the_api_routes():
    # Guards against the audit passing on an empty walk (e.g. included
    # routers invisible to it, as with a naive app.routes loop on FastAPI 0.16x).
    guarded = {r.path for r in api_routes(app) if r.path not in PUBLIC_API_PATHS}
    assert {"/api/me", "/api/workers", "/api/workers/{user_id}"} <= guarded
    assert {
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/workers/me/location",
        "/api/offers/{offer_id}/accept",
        "/api/offers/{offer_id}/decline",
        "/api/dispatch/sweep",
    } <= guarded


def _roles(route: EffectiveRoute) -> set[str]:
    return {
        role.value
        for dep in _walk(route.dependant)
        for role in getattr(dep.call, REQUIRE_ROLE_MARKER, ())
    }


def test_dispatch_routes_require_the_prp_roles():
    expected = {
        ("POST", "/api/jobs"): {"dispatcher"},
        ("GET", "/api/jobs"): {"dispatcher", "company_admin"},
        ("PATCH", "/api/jobs/{job_id}"): {"dispatcher"},
        ("PUT", "/api/workers/me/location"): {"worker"},
        ("POST", "/api/offers/{offer_id}/accept"): {"worker"},
        ("POST", "/api/offers/{offer_id}/decline"): {"worker"},
        ("POST", "/api/dispatch/sweep"): {"company_admin"},
    }
    actual = {
        (method, route.path): _roles(route)
        for route in api_routes(app)
        for method in route.methods
        if (method, route.path) in expected
    }
    assert actual == expected


# --- self-checks: the audit must be able to fail ------------------------------------------


def test_audit_flags_an_unguarded_route():
    leaky = FastAPI()

    @leaky.get("/api/leak")
    def leak():
        return {}

    assert audit_routes(leaky) == ["GET /api/leak: missing require_role(...)"]


def test_audit_flags_guarded_route_using_bare_get_db():
    leaky = FastAPI()

    @leaky.get("/api/raw", dependencies=[Depends(require_role(*ALL_ROLES))])
    def raw(db: Session = Depends(get_db)):
        return {}

    assert audit_routes(leaky) == ["GET /api/raw: depends on bare get_db (use get_tenant_db)"]


def test_audit_accepts_guard_nested_in_router_dependencies():
    ok = FastAPI()
    router = APIRouter(prefix="/api/things", dependencies=[Depends(require_role(*ALL_ROLES))])

    @router.get("")
    def things(db: Session = Depends(get_tenant_db)):
        return []

    ok.include_router(router)
    assert audit_routes(ok) == []


def test_audit_sees_routes_of_nested_and_guarded_includes():
    leaky = FastAPI()
    inner = APIRouter(prefix="/api/inner")
    outer = APIRouter()

    @inner.get("/open")
    def open_route():
        return {}

    outer.include_router(inner)
    leaky.include_router(outer)
    guarded = APIRouter(prefix="/api/guarded")

    @guarded.get("")
    def guarded_route():
        return {}

    leaky.include_router(guarded, dependencies=[Depends(require_role(*ALL_ROLES))])
    assert audit_routes(leaky) == ["GET /api/inner/open: missing require_role(...)"]


def test_audit_flags_a_mounted_sub_app_under_api():
    parent = FastAPI()
    parent.mount("/api/v2", FastAPI())
    assert audit_routes(parent) == ["/api/v2: non-APIRoute under /api (Mount)"]


def test_audit_does_not_exempt_paths_outside_the_allowlist():
    leaky = FastAPI()

    @leaky.post("/api/auth/register")
    def register():
        return {}

    assert audit_routes(leaky) == ["POST /api/auth/register: missing require_role(...)"]
