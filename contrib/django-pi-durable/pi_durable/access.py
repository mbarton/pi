"""Authorization hooks. Override with dotted paths in settings:

    PI_DURABLE = {
        "CAN_CREATE_STORE": "myproject.durable.can_create_store",  # (request) -> bool
        "CAN_ACCESS_STORE": "myproject.durable.can_access_store",  # (request, store) -> bool
    }

Defaults: authenticated users create stores they own, and access only their own stores; superusers access all.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest
from django.utils.module_loading import import_string

from .models import Store


def default_can_create_store(request: HttpRequest) -> bool:
    return request.user.is_authenticated


def default_can_access_store(request: HttpRequest, store: Store) -> bool:
    user = request.user
    return user.is_authenticated and (user.is_superuser or store.owner_id == user.pk)


def _hook(name: str, default: object) -> object:
    path = getattr(settings, "PI_DURABLE", {}).get(name)
    return default if path is None else import_string(path)


def can_create_store(request: HttpRequest) -> bool:
    return bool(_hook("CAN_CREATE_STORE", default_can_create_store)(request))  # type: ignore[operator]


def can_access_store(request: HttpRequest, store: Store) -> bool:
    return bool(_hook("CAN_ACCESS_STORE", default_can_access_store)(request, store))  # type: ignore[operator]
