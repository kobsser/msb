import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
SECRET_KEY = os.getenv("SECRET_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

BOT_USER_ID = 8299996037

if not API_ID or not API_HASH:
    raise RuntimeError("API_ID and API_HASH environment variables are required.")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is required.")

if not ENCRYPTION_KEY:
    raise RuntimeError("ENCRYPTION_KEY environment variable is required.")