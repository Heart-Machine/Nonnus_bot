"""Make bot.py importable and pin the settings the tests rely on.

python-dotenv leaves variables that are already in the environment alone, so
whatever is set here wins over a developer's own .env file and the suite
behaves the same locally and in CI.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ["COOKIES_FILE"] = ""
os.environ["STORAGE_CHAT_ID"] = ""
os.environ["MAX_FILE_SIZE_MB"] = "50"
os.environ["PHOTO_MAX_FILE_SIZE_MB"] = "10"
os.environ["ENABLE_VIDEO_COMPRESSION"] = "true"
