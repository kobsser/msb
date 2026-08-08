import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
SECRET_KEY = os.getenv("SECRET_KEY", "default-secret-key")

BOT_USER_ID = 8299996037

DATA_DIR = "/app/data" if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENV") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "users.db")