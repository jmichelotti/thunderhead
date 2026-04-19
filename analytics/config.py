import os

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "388076d3d5c84671b9602ae56f73ac34")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "1201"))

TIMEZONE = os.environ.get("TIMEZONE", "America/Los_Angeles")
