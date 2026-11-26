"""Authentication, session, and user management for the web server."""

import asyncio
from datetime import timedelta
import logging
import secrets
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .oidc import (
    OIDCConfig,
    OIDCError,
    authorization_url,
    discover_endpoints,
    exchange_code,
    fetch_userinfo,
    id_token_picture,
    oidc_config_from_settings,
)
from .storage import BackupStore
from .templating import templates
from .users import User, UserRole, UserStore

_LOGGER = logging.getLogger(__name__)

SESSION_COOKIE = "retentacle_session"
_HEALTH_PATH = "/health"
_STATIC_PREFIX = "/static/"
_PUBLIC_PATHS = frozenset({"/health", "/oidc/login", "/oidc/callback"})
_SETUP_PATHS = frozenset({"/setup"})
_LOGIN_PATHS = frozenset({"/login"})
_OIDC_STATE_TTL = 300.0


def current_user(request: Request) -> User | None:
    """Return the signed-in user for this request, if any."""
    return getattr(request.state, "user", None)


def require_admin(request: Request) -> User:
    """Return the signed-in admin user, raising 401/403 otherwise."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    if user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Admin access is required for this action.",
        )
    return user


def password_login_allowed(settings: dict) -> bool:
    """Whether local email and password sign-in is currently enabled."""
    config = oidc_config_from_settings(settings)
    return not (config.enabled and config.disable_password_login)


def _parse_body(body: bytes) -> dict[str, str]:
    """Parse an urlencoded request body into single string values."""
    return {
        key: values[-1] if values else ""
        for key, values in parse_qs(
            body.decode("utf-8", errors="replace"), keep_blank_values=True
        ).items()
    }


async def _form(request: Request) -> dict[str, str]:
    """Parse the urlencoded form body of a request."""
    return _parse_body(await request.body())


def request_is_secure(request: Request) -> bool:
    """Whether the request arrived over HTTPS (including via proxy headers)."""
    return request.url.scheme == "https"


def start_session(users: UserStore, user: User) -> str:
    """Create a login session and return its token."""
    token = users.create_session(user.user_id)
    _LOGGER.info("User %s signed in", user.email)
    return token


def with_session_cookie(
    response: Response,
    token: str,
    max_age: timedelta,
    *,
    secure: bool = False,
) -> Response:
    """Attach the session cookie to the given response."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=int(max_age.total_seconds()),
        httponly=True,
        secure=bool(secure),
        samesite="lax",
        path="/",
    )
    return response


def clear_session_cookie(response: Response) -> Response:
    """Remove the session cookie from the given response."""
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return response


def user_management_context(users: UserStore, actor: User | None) -> dict:
    """Return the template context for the user management section."""
    listed = users.list_users() if actor is not None else []
    admin_count = sum(1 for user in listed if user.role is UserRole.ADMIN)
    actor_id = actor.user_id if actor is not None else None
    rows = [
        {
            "user": user,
            "is_current": user.user_id == actor_id,
            "can_delete": not (
                user.user_id == actor_id
                or (user.role is UserRole.ADMIN and admin_count <= 1)
            ),
            "can_demote": not (user.role is UserRole.ADMIN and admin_count <= 1),
        }
        for user in listed
    ]
    return {
        "user_rows": rows,
        "user_role_values": [role.value for role in UserRole],
        "admin_count": admin_count,
        "user_form": {"email": "", "role": UserRole.VIEWER.value},
    }


def _render_login(
    request: Request,
    store: BackupStore,
    *,
    error: str | None = None,
    email: str = "",
    status_code: int = 200,
):
    """Render the login page with the current sign-in options."""
    config = oidc_config_from_settings(store.get_settings())
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        status_code=status_code,
        context={
            "error": error,
            "email": email,
            "oidc_enabled": config.ready,
            "oidc_provider_name": config.provider_name or "your account provider",
            "password_login_enabled": password_login_allowed(store.get_settings()),
        },
    )


def _register_setup_routes(app: FastAPI, users: UserStore) -> None:
    """Register the first-run admin account setup routes."""

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        """Show the form to create the initial admin account."""
        return templates.TemplateResponse(
            request=request,
            name="setup.html",
            context={"error": None, "email": ""},
        )

    @app.post("/setup")
    async def create_admin_account(request: Request):
        """Create the initial admin account and sign into it."""
        if users.has_users():
            return RedirectResponse(url="/login", status_code=303)
        form = await _form(request)
        email = form.get("email", "")
        password = form.get("password", "")
        confirm = form.get("confirm_password", "")
        error: str | None = None
        user: User | None = None
        if password != confirm:
            error = "The two passwords do not match."
        else:
            try:
                user = users.create_user(email, password, UserRole.ADMIN)
            except ValueError as exc:
                error = str(exc)
        if error is not None or user is None:
            return templates.TemplateResponse(
                request=request,
                name="setup.html",
                status_code=400,
                context={"error": error, "email": email},
            )
        token = start_session(users, user)
        response = RedirectResponse(url="/", status_code=303)
        return with_session_cookie(
            response, token, users.session_max_age, secure=request_is_secure(request)
        )


def _register_login_routes(app: FastAPI, users: UserStore, store: BackupStore) -> None:
    """Register the login and logout routes."""

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        """Show the login form."""
        return _render_login(request, store)

    @app.post("/login")
    async def login(request: Request):
        """Validate the submitted credentials and start a session."""
        form = await _form(request)
        email = form.get("email", "")
        password = form.get("password", "")
        if not password_login_allowed(store.get_settings()):
            _LOGGER.warning("Rejected local login while password sign-in is disabled")
            return _render_login(
                request,
                store,
                error="Password sign-in is disabled on this server.",
                email=email,
                status_code=403,
            )
        user = users.verify_credentials(email, password)
        if user is None:
            _LOGGER.warning("Failed login attempt for email: %r", email)
            return _render_login(
                request,
                store,
                error="Invalid email or password.",
                email=email,
                status_code=401,
            )
        token = start_session(users, user)
        response = RedirectResponse(url="/", status_code=303)
        return with_session_cookie(
            response, token, users.session_max_age, secure=request_is_secure(request)
        )

    @app.post("/logout")
    async def logout(request: Request):
        """End the current session and return to the login page."""
        users.delete_session(request.cookies.get(SESSION_COOKIE, ""))
        response = RedirectResponse(url="/login", status_code=303)
        return clear_session_cookie(response)


def _oidc_redirect_uri(request: Request, config: OIDCConfig) -> str:
    """Return the callback URI that the provider should redirect back to.

    Uses the configured callback URL verbatim so it matches the redirect URI
    registered at the provider; otherwise it derives one from the request.
    """
    if config.callback_url:
        return config.callback_url
    base = str(request.base_url).rstrip("/")
    return f"{base}/oidc/callback"


async def _oidc_verified_email(
    config: OIDCConfig, code: str, redirect_uri: str
) -> tuple[str, object]:
    """Return the verified email address and picture claim for a code.

    Identity is anchored on the userinfo endpoint: the access token was just
    exchanged at the provider's own token endpoint, so a successful userinfo
    call proves the profile belongs to the authenticated user. Providers that
    do not put the ``picture`` claim into their userinfo response (for example
    when the needed scope is not granted) are covered by the ID token fallback.
    """
    endpoints = await asyncio.to_thread(discover_endpoints, config.issuer)
    tokens = await asyncio.to_thread(
        exchange_code, config, endpoints, code, redirect_uri
    )
    access_token = str(tokens["access_token"])
    profile = await asyncio.to_thread(fetch_userinfo, endpoints, access_token)
    if profile.get("email_verified") is False:
        raise ValueError("The email address of your OIDC account is not verified.")
    email = str(profile.get("email") or "").strip()
    if not email:
        raise ValueError("The OIDC profile did not include an email address.")
    picture = profile.get("picture")
    if not picture:
        picture = id_token_picture(tokens.get("id_token"), email)
    return email, picture


def _oidc_local_user(users: UserStore, config: OIDCConfig, email: str) -> User | None:
    """Return the account for an OIDC email, creating it when allowed."""
    user = users.get_user_by_email(email)
    if user is None and config.auto_create_users:
        user = users.create_user(email, None, UserRole.VIEWER)
        _LOGGER.info("Created read-only account for OIDC user %s", email)
    return user


async def _oidc_start_login(
    request: Request,
    store: BackupStore,
    pending_states: dict[str, tuple[float, str]],
):
    """Return the redirect to the provider, or the login page on failure."""
    config = oidc_config_from_settings(store.get_settings())
    if not config.ready:
        return _render_login(
            request,
            store,
            error="OpenID Connect sign-in is not configured.",
            status_code=400,
        )
    try:
        endpoints = await asyncio.to_thread(discover_endpoints, config.issuer)
    except OIDCError as exc:
        _LOGGER.warning("OIDC discovery failed: %s", exc)
        return _render_login(request, store, error=str(exc), status_code=400)
    now = time.monotonic()
    for key in [key for key, (expires, _) in pending_states.items() if expires <= now]:
        pending_states.pop(key, None)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    redirect_uri = _oidc_redirect_uri(request, config)
    pending_states[state] = (now + _OIDC_STATE_TTL, redirect_uri)
    url = authorization_url(config, endpoints, redirect_uri, state, nonce)
    return RedirectResponse(url=url, status_code=302)


async def _oidc_process_callback(
    request: Request,
    store: BackupStore,
    users: UserStore,
    pending_states: dict[str, tuple[float, str]],
    *,
    code: str,
    state: str,
    error: str,
):
    """Resolve an OIDC callback into a session or a login page failure."""
    config = oidc_config_from_settings(store.get_settings())
    stored = pending_states.pop(state, None) if state else None
    if not config.ready:
        return _render_login(
            request,
            store,
            error="OpenID Connect sign-in is not configured.",
            status_code=400,
        )
    if error:
        return _render_login(
            request,
            store,
            error=f"The OIDC provider reported an error: {error}.",
            status_code=400,
        )
    if not code or stored is None or stored[0] <= time.monotonic():
        return _render_login(
            request,
            store,
            error="The sign-in attempt is invalid or expired. Try again.",
            status_code=400,
        )
    try:
        email, picture = await _oidc_verified_email(config, code, stored[1])
        user = _oidc_local_user(users, config, email)
    except (OIDCError, ValueError) as exc:
        _LOGGER.warning("OIDC sign-in failed: %s", exc)
        return _render_login(request, store, error=str(exc), status_code=401)
    if user is None:
        return _render_login(
            request,
            store,
            error=f"No local account exists for {email}.",
            email=email,
            status_code=403,
        )
    # Keep the stored profile picture in sync with the provider's claim.
    users.set_picture(user.user_id, picture)
    token = start_session(users, user)
    response = RedirectResponse(url="/", status_code=303)
    return with_session_cookie(
        response, token, users.session_max_age, secure=request_is_secure(request)
    )


def _register_oidc_routes(app: FastAPI, users: UserStore, store: BackupStore) -> None:
    """Register the OpenID Connect login and callback routes."""
    # Issued states are remembered briefly, together with the redirect URI
    # used at login, so the callback can be matched to its login attempt.
    pending_states: dict[str, tuple[float, str]] = {}

    @app.get("/oidc/login")
    async def oidc_login(request: Request):
        """Start the OpenID Connect authorization code flow."""
        return await _oidc_start_login(request, store, pending_states)

    @app.get("/oidc/callback", response_class=HTMLResponse)
    async def oidc_callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ):
        """Complete the OpenID Connect flow and sign the user in."""
        return await _oidc_process_callback(
            request,
            store,
            users,
            pending_states,
            code=code,
            state=state,
            error=error,
        )


def _redirect_target(path: str, user: User | None, has_users: bool) -> str | None:
    """Return the path to redirect to, if this request may not continue."""
    if path == _HEALTH_PATH:
        return None
    if not has_users:
        # During first-run setup only the setup wizard is reachable.
        return None if path in _SETUP_PATHS else "/setup"
    if path in _PUBLIC_PATHS:
        return None
    if path in _SETUP_PATHS:
        return "/login"
    if path in _LOGIN_PATHS:
        return "/" if user is not None else None
    return None if user is not None else "/login"


def _register_middleware(app: FastAPI, users: UserStore) -> None:
    """Register the authentication middleware on the application."""

    @app.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        """Require setup or a valid login session for all protected routes."""
        path = request.url.path
        # Health checks and static assets, such as the logo shown on the
        # login and setup pages, are always public.
        if path == _HEALTH_PATH or path.startswith(_STATIC_PREFIX):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        user = users.resolve_session(token) if token else None
        request.state.user = user
        # Needed to keep this session alive when the user changes their own
        # password, while every other session of the account ends.
        request.state.session_token = token
        target = _redirect_target(path, user, users.has_users())
        if target is not None:
            return RedirectResponse(url=target, status_code=303)
        return await call_next(request)


def register_auth(app: FastAPI, users: UserStore, store: BackupStore) -> None:
    """Register authentication middleware, login pages, and OIDC routes."""
    _register_middleware(app, users)
    _register_setup_routes(app, users)
    _register_login_routes(app, users, store)
    _register_oidc_routes(app, users, store)
