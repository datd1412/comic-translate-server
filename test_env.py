from config import settings
import os
print("OS ENV:", os.environ.get("GEMINI_API_KEY"))
print("SETTINGS:", settings.GEMINI_API_KEY)
