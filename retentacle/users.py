"""User accounts, roles, and login sessions."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import threading
from typing import Any

from tinydb import Query, TinyDB

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
MAX_EMAIL_LENGTH = 254
PBKDF2_ITERATIONS = 240000
SESSION_MAX_AGE = timedelta(days=30)

_USERS_TABLE = "users"
_SESSIONS_TABLE = "sessions"
_HASH_NAME = "pbkdf2_sha256"
_DUMMY_PASSWORD = "dummy-password-for-timing-equality"

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PASSWORD_DIGIT_PATTERN = re.compile(r"[0-9]")
_PASSWORD_SPECIAL_PATTERN = re.compile(r"[^A-Za-z0-9]")

_SESSION_QUERY = Query()

# A single lock protects all stores. TinyDB does not support concurrent access
# to the same file from multiple instances, and the server reads while the
# login flow writes.
_LOCK = threading.RLock()


class UserRole(StrEnum):
    """The roles a user can have."""

    ADMIN = "admin"
    VIEWER = "viewer"

    @property
    def label(self) -> str:
        """Return a human-readable role name."""
        return "Administrator" if self is UserRole.ADMIN else "Read-only"

    @classmethod
    def parse(cls, value: object) -> UserRole:
        """Return the role for the given value, raising on unknown values."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            allowed = ", ".join(role.value for role in cls)
            raise ValueError(
                f"Invalid role: {value!r}. Choose one of: {allowed}."
            ) from exc


@dataclass(frozen=True)
class User:
    """A user account."""

    user_id: int
    email: str
    role: UserRole
    created_at: datetime
    picture: str | None = None


def _picture_url(value: object) -> str | None:
    """Return a safe absolute image URL, or None."""
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value.strip()
    return None


def validate_email(email: object) -> str:
    """Return a validated email address, raising ValueError when invalid."""
    value = str(email or "").strip()
    if not value:
        raise ValueError("Enter an email address.")
    if len(value) > MAX_EMAIL_LENGTH:
        raise ValueError(
            f"Email addresses cannot be longer than {MAX_EMAIL_LENGTH} characters."
        )
    if not _EMAIL_PATTERN.fullmatch(value):
        raise ValueError("Enter a valid email address, like user@example.com.")
    return value


def validate_password(password: object) -> str:
    """Return a validated password, raising ValueError when invalid."""
    value = "" if password is None else str(password)
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if len(value) > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"Passwords cannot be longer than {MAX_PASSWORD_LENGTH} characters."
        )
    if not _PASSWORD_DIGIT_PATTERN.search(value):
        raise ValueError("Passwords must include at least one digit.")
    if not _PASSWORD_SPECIAL_PATTERN.search(value):
        raise ValueError(
            "Passwords must include at least one special character, such as"
            " '!', '#' or '-'."
        )
    return value


def _hash_password(password: str, iterations: int, salt: bytes) -> str:
    """Return the encoded PBKDF2 hash for the given password and salt."""
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_HASH_NAME}${iterations}${salt.hex()}${digest.hex()}"


def _encode_password(password: str, iterations: int) -> str:
    """Return the encoded PBKDF2 hash for the given password."""
    return _hash_password(password, iterations, secrets.token_bytes(16))


def _verify_password(password: str, encoded: object) -> bool:
    """Check a password against its stored encoded hash."""
    try:
        name, raw_iterations, raw_salt, raw_digest = str(encoded).split("$")
        if name != _HASH_NAME:
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(raw_salt)
        digest = bytes.fromhex(raw_digest)
    except ValueError:
        return False
    computed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, max(iterations, 1)
    )
    return hmac.compare_digest(computed, digest)


def _token_hash(token: str) -> str:
    """Return the storage key for a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_from_document(document: Any) -> User | None:
    """Convert a users table document into a User, if valid."""
    user_id = getattr(document, "doc_id", None)
    # Older databases store the identity under "username"; accept it as the
    # email so existing accounts keep working.
    email = document.get("email") or document.get("username")
    if user_id is None or not email:
        return None
    try:
        role = UserRole.parse(document.get("role"))
    except ValueError:
        return None
    created_at_raw = document.get("created_at")
    try:
        created_at = datetime.fromisoformat(str(created_at_raw))
    except ValueError:
        created_at = datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return User(
        user_id=int(user_id),
        email=str(email),
        role=role,
        created_at=created_at,
        picture=_picture_url(document.get("picture")),
    )


class UserStore:
    """Persistent storage for user accounts and login sessions."""

    def __init__(
        self,
        path: Path | str,
        *,
        pbkdf2_iterations: int = PBKDF2_ITERATIONS,
        session_max_age: timedelta = SESSION_MAX_AGE,
    ):
        """Initialise the store."""
        self._path = Path(path)
        self._pbkdf2_iterations = pbkdf2_iterations
        self._session_max_age = session_max_age

    @property
    def path(self) -> Path:
        """Get the path to the user database."""
        return self._path

    @property
    def session_max_age(self) -> timedelta:
        """Get the lifetime of login sessions."""
        return self._session_max_age

    def has_users(self) -> bool:
        """Whether at least one user account exists."""
        return bool(self.list_users())

    def list_users(self) -> list[User]:
        """Return all user accounts, sorted by email address."""
        with _LOCK:
            if not self._path.exists():
                return []
            db = TinyDB(self._path)
            try:
                documents = db.table(_USERS_TABLE).all()
            finally:
                db.close()
        users = [
            user
            for document in documents
            if (user := _user_from_document(document)) is not None
        ]
        return sorted(users, key=lambda user: user.email.casefold())

    def get_user(self, user_id: int) -> User | None:
        """Return a single user account by ID."""
        for user in self.list_users():
            if user.user_id == user_id:
                return user
        return None

    def get_user_by_email(self, email: object) -> User | None:
        """Return the user account for an email address, ignoring case."""
        candidate = str(email or "").strip().casefold()
        if not candidate:
            return None
        for user in self.list_users():
            if user.email.casefold() == candidate:
                return user
        return None

    def create_user(self, email: object, password: object | None, role: object) -> User:
        """Create a new user account and return it.

        Passing ``None`` as the password creates an account without local
        password credentials, for example one managed via OpenID Connect.
        """
        validated_email = validate_email(email)
        validated_role = UserRole.parse(role)
        password_hash = (
            None
            if password is None
            else _encode_password(validate_password(password), self._pbkdf2_iterations)
        )
        now = datetime.now(UTC)
        with _LOCK:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            db = TinyDB(self._path)
            try:
                documents = db.table(_USERS_TABLE).all()
                existing = [
                    str(document.get("email") or document.get("username", ""))
                    for document in documents
                ]
                if any(
                    name.casefold() == validated_email.casefold() for name in existing
                ):
                    raise ValueError("That email address is already in use.")
                document = {
                    "email": validated_email,
                    "password_hash": password_hash,
                    "role": validated_role.value,
                    "created_at": now.isoformat(),
                }
                doc_id = db.table(_USERS_TABLE).insert(document)
            finally:
                db.close()
        return User(
            user_id=doc_id,
            email=validated_email,
            role=validated_role,
            created_at=now,
        )

    def delete_user(self, user_id: int, *, acting_user_id: int | None = None) -> None:
        """Delete a user account and all of its sessions."""
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("User not found.")
        if acting_user_id is not None and user_id == acting_user_id:
            raise ValueError("You cannot delete your own account.")
        if user.role is UserRole.ADMIN and self._admin_count() <= 1:
            raise ValueError("You cannot delete the last admin account.")
        with _LOCK:
            db = TinyDB(self._path)
            try:
                db.table(_USERS_TABLE).remove(doc_ids=[user_id])
                db.table(_SESSIONS_TABLE).remove(_SESSION_QUERY.user_id == user_id)
            finally:
                db.close()

    def change_password(self, user_id: int, password: object) -> None:
        """Set a new password for the given user and end its sessions."""
        validated_password = validate_password(password)
        if self.get_user(user_id) is None:
            raise ValueError("User not found.")
        with _LOCK:
            db = TinyDB(self._path)
            try:
                db.table(_USERS_TABLE).update(
                    {
                        "password_hash": _encode_password(
                            validated_password, self._pbkdf2_iterations
                        )
                    },
                    doc_ids=[user_id],
                )
                db.table(_SESSIONS_TABLE).remove(_SESSION_QUERY.user_id == user_id)
            finally:
                db.close()

    def change_role(self, user_id: int, role: object) -> None:
        """Change the role of the given user."""
        validated_role = UserRole.parse(role)
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("User not found.")
        if (
            user.role is UserRole.ADMIN
            and validated_role is not UserRole.ADMIN
            and self._admin_count() <= 1
        ):
            raise ValueError("You cannot remove the role of the last admin account.")
        with _LOCK:
            db = TinyDB(self._path)
            try:
                db.table(_USERS_TABLE).update(
                    {"role": validated_role.value}, doc_ids=[user_id]
                )
            finally:
                db.close()

    def set_picture(self, user_id: int, picture: object) -> None:
        """Store the profile picture URL from the identity provider.

        Values that are not absolute http(s) URLs, including empty ones,
        clear the stored picture so a withdrawn claim does not linger.
        """
        with _LOCK:
            db = TinyDB(self._path)
            try:
                db.table(_USERS_TABLE).update(
                    {"picture": _picture_url(picture)}, doc_ids=[user_id]
                )
            finally:
                db.close()

    def verify_credentials(self, email: object, password: object) -> User | None:
        """Return the user matching the given credentials, if any."""
        candidate = str(email or "").strip().casefold()
        password_value = "" if password is None else str(password)
        with _LOCK:
            if not self._path.exists():
                _hash_password(_DUMMY_PASSWORD, self._pbkdf2_iterations, b"\x00" * 16)
                return None
            db = TinyDB(self._path)
            try:
                documents = db.table(_USERS_TABLE).all()
            finally:
                db.close()
        match: User | None = None
        password_hash = None
        for document in documents:
            identity = document.get("email") or document.get("username", "")
            if str(identity).casefold() == candidate:
                user = _user_from_document(document)
                if user is not None:
                    match = user
                    password_hash = document.get("password_hash")
                break
        if match is None or password_hash is None:
            # Spend the same time as a real verification would.
            _hash_password(_DUMMY_PASSWORD, self._pbkdf2_iterations, b"\x00" * 16)
            return None
        if not _verify_password(password_value, password_hash):
            return None
        return match

    def create_session(self, user_id: int) -> str:
        """Create a login session for the given user and return its token."""
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(UTC)
        with _LOCK:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            db = TinyDB(self._path)
            try:
                self._purge_expired_sessions(db, created_at)
                db.table(_SESSIONS_TABLE).insert(
                    {
                        "token_hash": _token_hash(token),
                        "user_id": user_id,
                        "created_at": created_at.isoformat(),
                        "expires_at": (created_at + self._session_max_age).isoformat(),
                    }
                )
            finally:
                db.close()
        return token

    def resolve_session(self, token: object) -> User | None:
        """Return the user of a valid session token, if any."""
        if not token:
            return None
        key = _token_hash(str(token))
        now = datetime.now(UTC)
        with _LOCK:
            if not self._path.exists():
                return None
            db = TinyDB(self._path)
            try:
                document = db.table(_SESSIONS_TABLE).get(
                    _SESSION_QUERY.token_hash == key
                )
                if document is None:
                    return None
                expires_at = self._session_expiry(document)
                if expires_at is not None and expires_at <= now:
                    db.table(_SESSIONS_TABLE).remove(_SESSION_QUERY.token_hash == key)
                    return None
                user_id = document.get("user_id")
                user_document = db.table(_USERS_TABLE).get(doc_id=user_id)
                if user_document is None:
                    db.table(_SESSIONS_TABLE).remove(_SESSION_QUERY.token_hash == key)
                    return None
                return _user_from_document(user_document)
            finally:
                db.close()

    def delete_session(self, token: object) -> None:
        """Remove the session for the given token, if it exists."""
        if not token:
            return
        key = _token_hash(str(token))
        with _LOCK:
            if not self._path.exists():
                return
            db = TinyDB(self._path)
            try:
                db.table(_SESSIONS_TABLE).remove(_SESSION_QUERY.token_hash == key)
            finally:
                db.close()

    def _admin_count(self) -> int:
        """Return the number of admin accounts."""
        return sum(1 for user in self.list_users() if user.role is UserRole.ADMIN)

    @staticmethod
    def _session_expiry(document: Any) -> datetime | None:
        """Return the expiry timestamp of a session document, if valid."""
        raw = document.get("expires_at")
        try:
            expiry = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry.astimezone(UTC)

    def _purge_expired_sessions(self, db: TinyDB, now: datetime) -> None:
        """Remove sessions that expire at or before the given time."""
        table = db.table(_SESSIONS_TABLE)
        expired_ids = [
            document.doc_id
            for document in table.all()
            if (expiry := self._session_expiry(document)) is None or expiry <= now
        ]
        if expired_ids:
            table.remove(doc_ids=expired_ids)
