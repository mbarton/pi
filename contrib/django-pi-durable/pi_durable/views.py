"""JSON-over-HTTP binding of `DurableStorage`, consumed by the TypeScript `HttpStorage` client.

    POST <prefix>/stores                 -> {"result": {"id": "<uuid>"}}
    POST <prefix>/stores/<uuid>/<method> -> {"result": ...}

The request body is a JSON object of named arguments. Absent results (`undefined` in TypeScript) are `null`. Failures
return {"error": {"type": "rejected" | "error", "message": ...}} with status 409 for `StorageRejected` and 400 otherwise.

Views are CSRF-exempt because clients are programs, not browsers. Requests must use `Content-Type: application/json`,
which a cross-site HTML form cannot send without a CORS preflight. Authentication is left to the host project (session,
token, or mTLS middleware); authorization goes through the `PI_DURABLE` access hooks in `access.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import access
from .delta import DeltaError
from .models import Store
from .storage import CURRENT, MAX_SAFE_INTEGER, DurableStorage, StorageError, StorageRejected


class _BadRequest(Exception):
    pass


def _int(args: dict[str, Any], name: str, optional: bool = False) -> int | None:
    value = args.get(name)
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_SAFE_INTEGER:
        raise _BadRequest(f"{name} must be a safe integer")
    return value


def _limit(args: dict[str, Any]) -> int:
    limit = _int(args, "limit")
    assert limit is not None
    if limit < 1:
        raise _BadRequest("limit must be positive")
    return limit


def _object(args: dict[str, Any], name: str, optional: bool = False) -> dict[str, Any] | None:
    value = args.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, dict):
        raise _BadRequest(f"{name} must be an object")
    return value


def _point(args: dict[str, Any]) -> int | str:
    at = args.get("at")
    return CURRENT if at == CURRENT else _int(args, "at")  # type: ignore[return-value]


def _scan(method: Callable[[dict[str, Any], int, dict[str, Any] | None], Any]) -> Callable[..., Any]:
    return lambda storage, args: method(storage, _object(args, "query"), _limit(args), _object(args, "cursor", True))


METHODS: dict[str, Callable[[DurableStorage, dict[str, Any]], Any]] = {
    "reserveIds": lambda s, a: s.reserve_ids(_int(a, "count")),
    "commit": lambda s, a: s.commit(a["writes"] if isinstance(a.get("writes"), list) else []),
    "conversation": lambda s, a: s.conversation(_int(a, "id")),
    "scanConversations": _scan(DurableStorage.scan_conversations),
    "entry": lambda s, a: s.entry(_int(a, "id"), _int(a, "conversationId", True)),
    "findLatestHeadMarker": lambda s, a: s.find_latest_head_marker(
        _int(a, "conversationId"), _int(a, "atOrBeforeEntryId", True)
    ),
    "scanEntries": _scan(DurableStorage.scan_entries),
    "task": lambda s, a: s.task(_int(a, "id")),
    "scanTasks": _scan(DurableStorage.scan_tasks),
    "submission": lambda s, a: s.submission(_int(a, "id")),
    "scanSubmissions": _scan(DurableStorage.scan_submissions),
    "submissionByRequest": lambda s, a: s.submission_by_request(_int(a, "conversationId"), str(a["requestId"])),
    "findDocument": lambda s, a: s.find_document(_object(a, "address"), _point(a)),
    "document": lambda s, a: s.document(_int(a, "id"), _point(a)),
    "scanDocuments": _scan(DurableStorage.scan_documents),
}


def _error(kind: str, message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": {"type": kind, "message": message}}, status=status)


def _arguments(request: HttpRequest) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise _BadRequest("Content-Type must be application/json")
    try:
        args = json.loads(request.body or b"{}")
    except ValueError as error:
        raise _BadRequest(f"Invalid JSON: {error}") from None
    if not isinstance(args, dict):
        raise _BadRequest("Request body must be a JSON object")
    return args


@csrf_exempt
@require_POST
def create_store(request: HttpRequest) -> JsonResponse:
    try:
        args = _arguments(request)
    except _BadRequest as error:
        return _error("error", str(error), 400)
    if not access.can_create_store(request):
        return _error("error", "Forbidden", 403)
    label = args.get("label")
    store = Store.objects.create(
        owner=request.user if request.user.is_authenticated else None,
        label=label if isinstance(label, str) else "",
    )
    return JsonResponse({"result": {"id": str(store.id)}}, status=201)


@csrf_exempt
@require_POST
def call(request: HttpRequest, store_id: str, method: str) -> JsonResponse:
    handler = METHODS.get(method)
    if handler is None:
        return _error("error", f"Unknown method: {method}", 404)
    store = Store.objects.filter(pk=store_id).first()
    # Unknown and forbidden stores look the same, so store IDs cannot be probed.
    if store is None or not access.can_access_store(request, store):
        return _error("error", "Unknown store", 404)
    try:
        result = handler(DurableStorage(store), _arguments(request))
    except StorageRejected as error:
        return _error("rejected", str(error), 409)
    except (StorageError, DeltaError, _BadRequest, KeyError, TypeError) as error:
        message = f"Missing argument: {error}" if isinstance(error, KeyError) else str(error)
        return _error("error", message, 400)
    return JsonResponse({"result": result})
