import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")

PANEL_PASSWORD = os.getenv("PANEL_PASSWORD")
SECRET_KEY = os.getenv("SECRET_KEY")

if not PANEL_PASSWORD:
    raise RuntimeError("PANEL_PASSWORD environment variable is required")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is required")

if not API_ID or not API_HASH:
    raise RuntimeError("API_ID and API_HASH environment variables are required")

# Hardcoded as requested
BOT_USER_ID = 8299996037

DATA_DIR = "/app/data" if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENV") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "users.db")