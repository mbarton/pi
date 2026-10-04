import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from pi_durable.models import Store


@override_settings(PI_DURABLE={})
class AccessTests(TestCase):
    def post(self, path, body=None):
        return self.client.post(f"/durable/{path}", data=json.dumps(body or {}), content_type="application/json")

    def test_default_hooks_scope_stores_to_their_owner(self):
        self.assertEqual(self.post("stores").status_code, 403)
        users = get_user_model()
        owner = users.objects.create_user("owner")
        other = users.objects.create_user("other")
        self.client.force_login(owner)
        response = self.post("stores", {"label": "mine"})
        self.assertEqual(response.status_code, 201)
        store_id = response.json()["result"]["id"]
        self.assertEqual(Store.objects.get(pk=store_id).owner, owner)
        self.assertEqual(self.post(f"stores/{store_id}/conversation", {"id": 1}).json(), {"result": None})
        self.client.force_login(other)
        self.assertEqual(self.post(f"stores/{store_id}/conversation", {"id": 1}).status_code, 404)

    def test_requires_json_and_reports_storage_errors(self):
        user = get_user_model().objects.create_user("owner")
        self.client.force_login(user)
        store_id = self.post("stores").json()["result"]["id"]
        form = self.client.post(f"/durable/stores/{store_id}/conversation", data={"id": "1"})
        self.assertEqual(form.status_code, 400)
        response = self.post(f"stores/{store_id}/scanEntries", {"query": {"conversationId": 9}, "limit": 10})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], {"type": "error", "message": "Unknown conversation: 9"})
