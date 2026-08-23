"""
Render entry point for Test Tuzuvchi.

The Telegram webhook and health endpoint run in the SAME FastAPI process.
There is no Telegram getUpdates/long-polling background process.
"""
from bot import web_app as app
