from __future__ import annotations

import os
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("JOBENGINE_DATA_DIR", str(BASE_DIR / "data")))
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    secret_path = DATA_DIR / ".django-secret"
    try:
        with secret_path.open("x", encoding="utf-8") as secret_file:
            secret_file.write(secrets.token_urlsafe(64))
    except FileExistsError:
        pass
    SECRET_KEY = secret_path.read_text(encoding="utf-8")

ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
INSTALLED_APPS = ["django.contrib.staticfiles", "jobengine.web"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "jobengine.web.middleware.PrivateWorkspaceMiddleware",
]
ROOT_URLCONF = "jobengine.web.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "jobengine.web.wsgi.application"
# Cookies carry only an opaque workspace ID; candidate data and task state stay in SQLite.
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_NAME = "jobengine_workspace"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 180
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / ".scratch" / "static"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TIME_ZONE = "America/New_York"
USE_TZ = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 12_000_000
FILE_UPLOAD_MAX_MEMORY_SIZE = 1_000_000
DATA_UPLOAD_MAX_NUMBER_FILES = 4
# The legacy library is only available on loopback in development.
LOCAL_LIBRARY = DEBUG and os.getenv("JOBENGINE_LOCAL_LIBRARY", "1") == "1"
