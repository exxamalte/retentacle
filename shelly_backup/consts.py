"""Application constants."""

from typing import Final

APPLICATION_NAME: Final = "Shelly Backup"
SHELLY_MDNS_SERVICE_TYPE: Final = "_shelly._tcp.local."
SHELLY_HTTP_MDNS_SERVICE_TYPE: Final = "_http._tcp.local."

DEFAULT_INTERVAL: Final = 3600
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8080
DATABASE_FILENAME: Final = "shelly_backup.json"
USERS_FILENAME: Final = "shelly_backup_users.json"
