"""Tests for the user account storage module."""

from datetime import UTC, datetime, timedelta
import tempfile

import pytest

from shelly_backup.users import (
    User,
    UserRole,
    UserStore,
    validate_password,
    validate_username,
)


def _store(tmpdir, **kwargs):
    """Create a user store with cheap password hashing."""
    return UserStore(f"{tmpdir}/users.json", pbkdf2_iterations=1, **kwargs)


class TestValidation:
    """Tests for the username and password validation helpers."""

    def test_validate_username(self):
        """Test that valid usernames are accepted and trimmed."""
        assert validate_username("  admin_1@corp-x ") == "admin_1@corp-x"

    def test_validate_username_rejects_empty(self):
        """Test that empty usernames are rejected."""
        with pytest.raises(ValueError, match="Enter a username"):
            validate_username("   ")

    def test_validate_username_rejects_too_long(self):
        """Test that long usernames are rejected."""
        with pytest.raises(ValueError, match="longer than"):
            validate_username("u" * 100)

    def test_validate_username_rejects_invalid_characters(self):
        """Test that usernames with invalid characters are rejected."""
        with pytest.raises(ValueError, match="letters"):
            validate_username("admin name!")

    def test_validate_username_rejects_invalid_first_character(self):
        """Test that usernames must start with a letter or digit."""
        with pytest.raises(ValueError, match="letters"):
            validate_username("_admin")

    def test_validate_password(self):
        """Test that a long enough password is accepted."""
        assert validate_password("password-1234") == "password-1234"

    def test_validate_password_rejects_short(self):
        """Test that short passwords are rejected."""
        with pytest.raises(ValueError, match="at least 12"):
            validate_password("short")

    def test_validate_password_rejects_missing_digit(self):
        """Test that passwords without a digit are rejected."""
        with pytest.raises(ValueError, match="at least one digit"):
            validate_password("all-letters-and-symbols")

    def test_validate_password_rejects_missing_special(self):
        """Test that passwords without a special character are rejected."""
        with pytest.raises(ValueError, match="special character"):
            validate_password("lettersanddigits1234")

    def test_validate_password_rejects_long(self):
        """Test that extremely long passwords are rejected."""
        with pytest.raises(ValueError, match="longer than"):
            validate_password("s" * 200)

    def test_user_role_parse(self):
        """Test parsing roles from strings and enum members."""
        assert UserRole.parse("admin") is UserRole.ADMIN
        assert UserRole.parse(" VIEWER ") is UserRole.VIEWER
        assert UserRole.parse(UserRole.ADMIN) is UserRole.ADMIN

    def test_user_role_parse_rejects_unknown(self):
        """Test that unknown roles are rejected."""
        with pytest.raises(ValueError, match="Invalid role"):
            UserRole.parse("superuser")

    def test_user_role_label(self):
        """Test the human-readable role labels."""
        assert UserRole.ADMIN.label == "Administrator"
        assert UserRole.VIEWER.label == "Read-only"


class TestUserStore:
    """Tests for the user store."""

    def test_has_users_without_accounts(self):
        """Test that an empty store reports no users."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)

            assert store.has_users() is False
            assert store.list_users() == []

    def test_create_and_list_users(self):
        """Test creating users and listing them sorted by username."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            viewer = store.create_user("viewer", "password-1234", UserRole.VIEWER)
            admin = store.create_user("admin", "password-1234", UserRole.ADMIN)

            users = store.list_users()

            assert [user.username for user in users] == ["admin", "viewer"]
            assert admin.role is UserRole.ADMIN
            assert viewer.role is UserRole.VIEWER
            assert store.has_users() is True
            assert store.get_user(viewer.user_id) == viewer
            assert store.get_user(999) is None

    def test_create_user_validates_input(self):
        """Test that invalid user data is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)

            with pytest.raises(ValueError, match="Enter a username"):
                store.create_user("", "password-1234", UserRole.VIEWER)
            with pytest.raises(ValueError, match="at least 12"):
                store.create_user("user", "short", UserRole.VIEWER)
            with pytest.raises(ValueError, match="Invalid role"):
                store.create_user("user", "password-1234", "root")

    def test_create_user_rejects_duplicate_username_case_insensitive(self):
        """Test that usernames are unique regardless of case."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            store.create_user("Admin", "password-1234", UserRole.ADMIN)

            with pytest.raises(ValueError, match="already taken"):
                store.create_user("admin", "password-1234", UserRole.VIEWER)

    def test_password_is_stored_hashed(self):
        """Test that plain passwords never reach the database file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            store.create_user("admin", "password-1234", UserRole.ADMIN)

            with open(f"{tmpdir}/users.json") as handle:
                contents = handle.read()

            assert "password-1234" not in contents
            assert "pbkdf2_sha256" in contents

    def test_verify_credentials(self):
        """Test verifying correct and incorrect credentials."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            created = store.create_user("admin", "password-1234", UserRole.ADMIN)

            assert store.verify_credentials("admin", "password-1234") == created
            assert store.verify_credentials("ADMIN ", "password-1234") == created
            assert store.verify_credentials("admin", "wrong-password") is None
            assert store.verify_credentials("ghost", "password-1234") is None

    def test_verify_credentials_without_database(self):
        """Test that verifying against a missing store fails gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)

            assert store.verify_credentials("admin", "password-1234") is None

    def test_change_password(self):
        """Test that a password change takes effect immediately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)

            store.change_password(user.user_id, "password-5678")

            assert store.verify_credentials("admin", "password-1234") is None
            assert store.verify_credentials("admin", "password-5678") is not None

    def test_change_password_rejects_invalid(self):
        """Test that password rules also apply to changes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)

            with pytest.raises(ValueError, match="at least 12"):
                store.change_password(user.user_id, "short")
            with pytest.raises(ValueError, match="User not found"):
                store.change_password(999, "password-1234")

    def test_change_role(self):
        """Test changing the role of a user."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            store.create_user("second", "password-1234", UserRole.ADMIN)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)

            store.change_role(user.user_id, "viewer")

            assert store.get_user(user.user_id).role is UserRole.VIEWER

    def test_change_role_rejects_removing_last_admin(self):
        """Test that the last admin cannot be demoted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            admin = store.create_user("admin", "password-1234", UserRole.ADMIN)

            with pytest.raises(ValueError, match="last admin"):
                store.change_role(admin.user_id, "viewer")

            assert store.get_user(admin.user_id).role is UserRole.ADMIN

    def test_delete_user(self):
        """Test deleting a user account."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            admin = store.create_user("admin", "password-1234", UserRole.ADMIN)
            other = store.create_user("other", "password-1234", UserRole.ADMIN)

            store.delete_user(other.user_id, acting_user_id=admin.user_id)

            assert store.get_user(other.user_id) is None

    def test_delete_user_rejects_unknown_and_self(self):
        """Test that unknown and own accounts cannot be deleted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            admin = store.create_user("admin", "password-1234", UserRole.ADMIN)

            with pytest.raises(ValueError, match="not found"):
                store.delete_user(999)
            with pytest.raises(ValueError, match="own account"):
                store.delete_user(admin.user_id, acting_user_id=admin.user_id)

    def test_delete_user_rejects_last_admin(self):
        """Test that the last admin account cannot be deleted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            admin = store.create_user("admin", "password-1234", UserRole.ADMIN)
            other_admin = store.create_user("second", "password-1234", UserRole.ADMIN)

            store.delete_user(other_admin.user_id, acting_user_id=admin.user_id)

            with pytest.raises(ValueError, match="last admin"):
                store.delete_user(admin.user_id)


class TestSessions:
    """Tests for login sessions."""

    def test_create_and_resolve_session(self):
        """Test that a session token resolves to its user."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)

            token = store.create_session(user.user_id)

            assert store.resolve_session(token) == user
            assert store.resolve_session("nonsense") is None
            assert store.resolve_session("") is None
            assert store.resolve_session(None) is None

    def test_resolve_session_without_database(self):
        """Test that resolving against a missing store fails gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)

            assert store.resolve_session("token") is None

    def test_delete_session(self):
        """Test ending a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)
            token = store.create_session(user.user_id)

            store.delete_session(token)

            assert store.resolve_session(token) is None
            store.delete_session(token)
            store.delete_session("")

    def test_expired_session_is_rejected(self):
        """Test that sessions past their lifetime are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir, session_max_age=timedelta(seconds=-10))
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)
            token = store.create_session(user.user_id)

            assert store.resolve_session(token) is None

    def test_session_of_deleted_user_is_rejected(self):
        """Test that deleting a user invalidates its sessions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            admin = store.create_user("admin", "password-1234", UserRole.ADMIN)
            other = store.create_user("other", "password-1234", UserRole.VIEWER)
            token = store.create_session(other.user_id)

            store.delete_user(other.user_id, acting_user_id=admin.user_id)

            assert store.resolve_session(token) is None

    def test_password_change_invalidates_sessions(self):
        """Test that changing a password ends the sessions of that user."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)
            token = store.create_session(user.user_id)

            store.change_password(user.user_id, "password-5678")

            assert store.resolve_session(token) is None

    def test_tokens_are_stored_hashed(self):
        """Test that raw session tokens are not persisted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)
            token = store.create_session(user.user_id)
            with open(f"{tmpdir}/users.json") as handle:
                contents = handle.read()

            assert token not in contents

    def test_created_user_dataclass_fields(self):
        """Test that created users carry sane metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(tmpdir)
            user = store.create_user("admin", "password-1234", UserRole.ADMIN)

            assert isinstance(user, User)
            assert user.username == "admin"
            assert user.created_at.tzinfo is not None
            assert user.created_at <= datetime.now(UTC) + timedelta(minutes=1)
