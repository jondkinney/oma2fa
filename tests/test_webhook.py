from __future__ import annotations

import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from oma2fa.service import Oma2FAService
from oma2fa.store import RuntimeStore, StoreError
from oma2fa.webhook import (
    MAX_WEBHOOK_WORKERS,
    WebhookConfig,
    WebhookConfigError,
    WebhookServer,
)
from tests.test_store import Clock

TOKEN = "fixture-token-with-at-least-32-bytes"


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = RuntimeStore(Path(self.temporary.name) / "runtime", clock=self.clock)
        self.service = Oma2FAService(self.store, clock=self.clock)
        config = WebhookConfig(True, "127.0.0.1", 0, TOKEN)
        self.server = WebhookServer(
            self.service,
            config,
            allow_ephemeral_port=True,
            maintenance_seconds=0.01,
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str = "/v1/ingest",
        *,
        body: bytes = b"",
        token: str = TOKEN,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, object]]:
        host, port = self.server.address
        connection = http.client.HTTPConnection(host, port, timeout=2)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        status = response.status
        connection.close()
        return status, payload

    @staticmethod
    def payload(code: str = "123456") -> bytes:
        return json.dumps(
            {
                "sender": "Example",
                "body": f"Your verification code is {code}",
                "source": "ios-shortcuts",
                "timestamp": 1_700_000_000,
                "message_id": f"fixture-{code}",
            }
        ).encode()

    def test_authenticated_post_uses_generic_ingest_without_echoing_secret(self) -> None:
        status, response = self.request("POST", body=self.payload())
        self.assertEqual(status, 202)
        self.assertTrue(response["ok"])
        self.assertTrue(response["accepted"])
        self.assertNotIn("123456", json.dumps(response))
        records = self.store.list()
        self.assertEqual(records[0].code, "123456")
        self.assertEqual(records[0].source, "webhook/ios-shortcuts")

        duplicate_status, duplicate = self.request("POST", body=self.payload())
        self.assertEqual(duplicate_status, 200)
        self.assertEqual(duplicate["reason"], "duplicate")

    def test_bad_auth_method_path_media_type_and_size_are_rejected(self) -> None:
        status, _ = self.request("POST", body=self.payload(), token="wrong")
        self.assertEqual(status, 401)
        status, _ = self.request("GET")
        self.assertEqual(status, 405)
        status, _ = self.request("POST", "/other", body=self.payload())
        self.assertEqual(status, 404)
        status, _ = self.request(
            "POST",
            body=self.payload(),
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        status, _ = self.request("POST", body=b"x" * 16_385)
        self.assertEqual(status, 413)
        self.assertEqual(self.store.list(), [])

    def test_invalid_json_and_field_types_are_rejected_generically(self) -> None:
        for body in (
            b"not json",
            b"[]",
            b'{"sender":"Example","body":7}',
            b'{"sender":7,"body":"fixture"}',
        ):
            with self.subTest(body=body):
                status, response = self.request("POST", body=body)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], "invalid request")

    def test_store_failure_returns_generic_service_unavailable(self) -> None:
        with patch.object(
            self.service,
            "ingest",
            side_effect=StoreError("private fixture path and detail"),
        ):
            status, response = self.request("POST", body=self.payload())
        self.assertEqual(status, 503)
        self.assertEqual(response, {"ok": False, "error": "temporarily unavailable"})
        self.assertNotIn("private fixture", json.dumps(response))

    def test_maintenance_physically_prunes_expired_codes(self) -> None:
        status, _ = self.request("POST", body=self.payload())
        self.assertEqual(status, 202)
        self.clock.value += 601
        deadline = time.monotonic() + 1
        records: list[object] = [object()]
        while time.monotonic() < deadline:
            state = json.loads(self.store.path.read_text())
            records = state["records"]
            if not records:
                break
            time.sleep(0.01)
        self.assertEqual(records, [])

    def test_worker_slots_are_released_after_requests(self) -> None:
        for _index in range(3):
            status, _ = self.request("POST", body=b"{}")
            self.assertEqual(status, 400)
        slots = self.server._server._worker_slots
        deadline = time.monotonic() + 1
        acquired: list[bool] = []
        while time.monotonic() < deadline:
            acquired = [slots.acquire(blocking=False) for _index in range(MAX_WEBHOOK_WORKERS)]
            for did_acquire in acquired:
                if did_acquire:
                    slots.release()
            if all(acquired):
                break
            time.sleep(0.01)
        self.assertTrue(all(acquired))
        acquired = [slots.acquire(blocking=False) for _index in range(MAX_WEBHOOK_WORKERS)]
        self.assertTrue(all(acquired))
        self.assertFalse(slots.acquire(blocking=False))
        for _index in range(MAX_WEBHOOK_WORKERS):
            slots.release()


class WebhookConfigTests(unittest.TestCase):
    def test_disabled_by_default_and_env_can_enable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OMA2FA_WEBHOOK_TOKEN_FILE": "/missing/stale-token",
                "OMA2FA_WEBHOOK_PORT": "not-a-port",
            },
            clear=True,
        ):
            self.assertFalse(WebhookConfig.from_env().enabled)
        with patch.dict(
            os.environ,
            {
                "OMA2FA_WEBHOOK_ENABLED": "1",
                "OMA2FA_WEBHOOK_TOKEN": TOKEN,
                "OMA2FA_WEBHOOK_BIND": "127.0.0.2",
                "OMA2FA_WEBHOOK_PORT": "9876",
            },
            clear=True,
        ):
            config = WebhookConfig.from_env()
        self.assertTrue(config.enabled)
        self.assertEqual(config.bind, "127.0.0.2")
        self.assertEqual(config.port, 9876)

    def test_token_file_is_private_single_open_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "token"
            token_file.write_text(TOKEN)
            os.chmod(token_file, 0o600)
            with patch.dict(
                os.environ,
                {
                    "OMA2FA_WEBHOOK_ENABLED": "1",
                    "OMA2FA_WEBHOOK_TOKEN_FILE": str(token_file),
                },
                clear=True,
            ):
                self.assertEqual(WebhookConfig.from_env().token, TOKEN)

            link = Path(temporary) / "link"
            link.symlink_to(token_file)
            with self.assertRaises(WebhookConfigError):
                WebhookConfig.from_env(force_enabled=True, token_file=str(link))

            os.chmod(token_file, 0o644)
            with self.assertRaises(WebhookConfigError):
                WebhookConfig.from_env(force_enabled=True, token_file=str(token_file))

    def test_enabled_webhook_requires_strong_token_and_valid_port(self) -> None:
        with self.assertRaises(WebhookConfigError):
            WebhookConfig(True, "127.0.0.1", 8765, "short").validate()
        with self.assertRaises(WebhookConfigError):
            WebhookConfig(True, "127.0.0.1", 0, TOKEN).validate()

    def test_non_loopback_bind_requires_explicit_vpn_transport(self) -> None:
        with self.assertRaisesRegex(WebhookConfigError, "TRANSPORT=vpn"):
            WebhookConfig(True, "100.82.77.125", 8765, TOKEN).validate()

        WebhookConfig(True, "100.82.77.125", 8765, TOKEN, "vpn").validate()
        WebhookConfig(True, "fd7a:115c:a1e0::153b:4d7d", 8765, TOKEN, "vpn").validate()

    def test_loopback_binds_need_no_transport_assertion(self) -> None:
        for bind in ("localhost", "127.0.0.1", "127.42.0.1", "::1"):
            with self.subTest(bind=bind):
                WebhookConfig(True, bind, 8765, TOKEN).validate()

    def test_wildcards_hostnames_and_unknown_transports_fail_closed(self) -> None:
        for bind in ("0.0.0.0", "::"):
            with (
                self.subTest(bind=bind),
                self.assertRaisesRegex(WebhookConfigError, "wildcard"),
            ):
                WebhookConfig(True, bind, 8765, TOKEN, "vpn").validate()
        with self.assertRaisesRegex(WebhookConfigError, "literal IP"):
            WebhookConfig(True, "oma2fa.example", 8765, TOKEN, "vpn").validate()
        with self.assertRaisesRegex(WebhookConfigError, "loopback.*vpn"):
            WebhookConfig(True, "127.0.0.1", 8765, TOKEN, "tls").validate()

    def test_vpn_transport_can_be_asserted_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OMA2FA_WEBHOOK_ENABLED": "1",
                "OMA2FA_WEBHOOK_TOKEN": TOKEN,
                "OMA2FA_WEBHOOK_BIND": "100.82.77.125",
                "OMA2FA_WEBHOOK_TRANSPORT": "VPN",
            },
            clear=True,
        ):
            config = WebhookConfig.from_env()
        self.assertEqual(config.transport, "vpn")


if __name__ == "__main__":
    unittest.main()
