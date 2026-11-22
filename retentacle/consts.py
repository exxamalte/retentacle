"""Application constants."""

from typing import Final

APPLICATION_NAME: Final = "Retentacle"
APPLICATION_TAGLINE: Final = "Tentacles for your Shelly backups."
PROJECT_URL: Final = "https://github.com/exxamalte/retentacle"
SHELLY_MDNS_SERVICE_TYPE: Final = "_shelly._tcp.local."
SHELLY_HTTP_MDNS_SERVICE_TYPE: Final = "_http._tcp.local."

DEFAULT_INTERVAL: Final = 3600
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8080
DATABASE_FILENAME: Final = "retentacle_backups.json"
SETTINGS_FILENAME: Final = "retentacle_settings.json"
USERS_FILENAME: Final = "retentacle_users.json"
