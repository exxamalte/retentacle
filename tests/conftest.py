"""Shared test configuration."""

import os

# Keep the suite offline and GitHub-rate-limit friendly: update checks are
# enabled explicitly in the tests that need them.
os.environ.setdefault("RETENTACLE_UPDATE_CHECK", "0")
