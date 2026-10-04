"""Minimal project for the app's tests and for running the TypeScript conformance suite against a live server."""

import os

SECRET_KEY = "pi-durable-test"
DEBUG = True
ALLOWED_HOSTS = ["*"]
USE_TZ = True
ROOT_URLCONF = "testproject.urls"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "pi_durable",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("PI_DURABLE_TEST_DB", ":memory:"),
    }
}
PI_DURABLE = {
    "CAN_CREATE_STORE": "testproject.access.allow",
    "CAN_ACCESS_STORE": "testproject.access.allow",
}
