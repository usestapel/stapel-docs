"""Scaffold example tests — replace alongside the ping example.

They demonstrate the three house test layers: HTTP endpoint, comm Function
under schema validation, and the settings seam.
"""
import pytest
from django.test import override_settings

from stapel_core.comm import call


@pytest.mark.django_db
class TestPingEndpoint:
    def test_ping(self, api_client):
        resp = api_client.get("/docs/api/v1/ping")
        assert resp.status_code == 200
        assert resp.json()["greeting"] == "pong"

    @override_settings(STAPEL_DOCS={"GREETING": "hi"})
    def test_greeting_is_a_setting(self, api_client):
        assert api_client.get("/docs/api/v1/ping").json()["greeting"] == "hi"


class TestPingFunction:
    def test_call_in_process(self):
        assert call("docs.ping", {}) == {"greeting": "pong"}
