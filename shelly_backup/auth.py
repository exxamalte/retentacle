"""Authentication, session, and user management for the web server."""

from datetime import timedelta
import logging
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .templating import templates
from .users import User, UserRole, UserStore

_LOGGER = logging.getLogger(__name__)

SESSION_COOKIE = "shelly_backup_session"
_PUBLIC_PATHS = frozenset({"/health"})
_SETUP_PATHS = frozenset({"/setup"})
_LOGIN_PATHS = frozenset({"/login"})


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


def start_session(users: UserStore, user: User) -> str:
    """Create a login session and return its token."""
    token = users.create_session(user.user_id)
    _LOGGER.info("User %s signed in", user.username)
    return token


def with_session_cookie(
    response: Response,
    token: str,
    max_age: timedelta,
) -> Response:
    """Attach the session cookie to the given response."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=int(max_age.total_seconds()),
        httponly=True,
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
        "user_form": {"username": "", "role": UserRole.VIEWER.value},
    }


def _register_setup_routes(app: FastAPI, users: UserStore) -> None:
    """Register the first-run admin account setup routes."""

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        """Show the form to create the initial admin account."""
        return templates.TemplateResponse(
            request=request,
            name="setup.html",
            context={"error": None, "username": ""},
        )

    @app.post("/setup")
    async def create_admin_account(request: Request):
        """Create the initial admin account and sign into it."""
        if users.has_users():
            return RedirectResponse(url="/login", status_code=303)
        form = await _form(request)
        username = form.get("username", "")
        password = form.get("password", "")
        confirm = form.get("confirm_password", "")
        error: str | None = None
        user: User | None = None
        if password != confirm:
            error = "The two passwords do not match."
        else:
            try:
                user = users.create_user(username, password, UserRole.ADMIN)
            except ValueError as exc:
                error = str(exc)
        if error is not None or user is None:
            return templates.TemplateResponse(
                request=request,
                name="setup.html",
                status_code=400,
                context={"error": error, "username": username},
            )
        token = start_session(users, user)
        response = RedirectResponse(url="/", status_code=303)
        return with_session_cookie(response, token, users.session_max_age)


def _register_login_routes(app: FastAPI, users: UserStore) -> None:
    """Register the login and logout routes."""

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        """Show the login form."""
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": None, "username": ""},
        )

    @app.post("/login")
    async def login(request: Request):
        """Validate the submitted credentials and start a session."""
        form = await _form(request)
        username = form.get("username", "")
        password = form.get("password", "")
        user = users.verify_credentials(username, password)
        if user is None:
            _LOGGER.warning("Failed login attempt for username: %r", username)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                status_code=401,
                context={
                    "error": "Invalid username or password.",
                    "username": username,
                },
            )
        token = start_session(users, user)
        response = RedirectResponse(url="/", status_code=303)
        return with_session_cookie(response, token, users.session_max_age)

    @app.post("/logout")
    async def logout(request: Request):
        """End the current session and return to the login page."""
        users.delete_session(request.cookies.get(SESSION_COOKIE, ""))
        response = RedirectResponse(url="/login", status_code=303)
        return clear_session_cookie(response)


def _redirect_target(path: str, user: User | None, has_users: bool) -> str | None:
    """Return the path to redirect to, if this request may not continue."""
    if path in _PUBLIC_PATHS:
        return None
    if not has_users:
        return None if path in _SETUP_PATHS else "/setup"
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
        token = request.cookies.get(SESSION_COOKIE, "")
        user = users.resolve_session(token) if token else None
        request.state.user = user
        target = _redirect_target(request.url.path, user, users.has_users())
        if target is not None:
            return RedirectResponse(url=target, status_code=303)
        return await call_next(request)


def register_auth(app: FastAPI, users: UserStore) -> None:
    """Register authentication middleware, setup, and login pages."""
    _register_middleware(app, users)
    _register_setup_routes(app, users)
    _register_login_routes(app, users)
