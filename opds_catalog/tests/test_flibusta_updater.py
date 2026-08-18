import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

from opds_catalog.flibusta_updater import (
    FlibustaUpdater,
    UpdateConfig,
    UpdaterAlreadyRunning,
    UpdaterError,
)
from opds_catalog.network import build_telegram_request, validate_proxy_url
from opds_catalog.process_lock import NonBlockingFileLock


def zip_bytes(filename="book.fb2", content=b"<FictionBook/>"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()


class FlibustaUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.destination = self.root / "library"
        self.state = self.root / "state.json"
        self.lock = self.root / "update.lock"

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **overrides):
        values = {
            "source_url": "https://flibusta.example/daily/",
            "destination": self.destination,
            "state_file": self.state,
            "lock_file": self.lock,
            "retries": 1,
        }
        values.update(overrides)
        return UpdateConfig(**values)

    @staticmethod
    def client_factory(handler, captured=None):
        transport = httpx.MockTransport(handler)

        def factory(**kwargs):
            if captured is not None:
                captured.update(kwargs)
            return httpx.Client(
                transport=transport,
                follow_redirects=kwargs.get("follow_redirects", True),
                timeout=kwargs.get("timeout"),
                headers=kwargs.get("headers"),
                trust_env=kwargs.get("trust_env", False),
            )

        return factory

    def test_downloads_valid_archives_atomically_and_skips_existing(self):
        existing = "f.fb2.000001-000010.zip"
        new = "f.fb2.000011-000020.zip"
        self.destination.mkdir()
        (self.destination / existing).write_bytes(zip_bytes())
        listing = (
            '<a href="../">parent</a>'
            f'<a href="{existing}">old</a>'
            f'<a href="{new}">new</a>'
            '<a href="https://evil.example/f.fb2.999-1000.zip">external</a>'
            '<a href="notes.txt">ignore</a>'
        )

        def handler(request):
            if request.url.path.endswith("/daily/"):
                return httpx.Response(200, text=listing, request=request)
            if request.url.path.endswith(new):
                return httpx.Response(200, content=zip_bytes(), request=request)
            return httpx.Response(404, request=request)

        updater = FlibustaUpdater(
            self.config(),
            client_factory=self.client_factory(handler),
        )
        result = updater.run()

        self.assertEqual(result.discovered, 2)
        self.assertEqual(result.existing, 1)
        self.assertEqual(result.downloaded, 1)
        self.assertTrue((self.destination / new).is_file())
        self.assertFalse((self.destination / f"{new}.part").exists())
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["result"]["files"], [new])

    def test_dry_run_does_not_download_or_publish(self):
        new = "f.fb2.000021-000030.zip"

        def handler(request):
            return httpx.Response(
                200,
                text=f'<a href="{new}">new</a>',
                request=request,
            )

        updater = FlibustaUpdater(
            self.config(),
            client_factory=self.client_factory(handler),
        )
        result = updater.run(dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertEqual(result.files, [new])
        self.assertFalse((self.destination / new).exists())
        self.assertEqual(
            json.loads(self.state.read_text(encoding="utf-8"))["status"],
            "dry-run",
        )

    def test_corrupt_archive_keeps_library_unchanged(self):
        good = "f.fb2.000031-000040.zip"
        broken = "f.fb2.000041-000050.zip"
        listing = f'<a href="{good}">good</a><a href="{broken}">bad</a>'

        def handler(request):
            if request.url.path.endswith("/daily/"):
                return httpx.Response(200, text=listing, request=request)
            if request.url.path.endswith(good):
                return httpx.Response(200, content=zip_bytes(), request=request)
            return httpx.Response(200, content=b"not-a-zip", request=request)

        updater = FlibustaUpdater(
            self.config(),
            client_factory=self.client_factory(handler),
        )
        with self.assertRaises(UpdaterError):
            updater.run()

        self.assertFalse((self.destination / good).exists())
        self.assertFalse((self.destination / broken).exists())
        self.assertEqual(
            json.loads(self.state.read_text(encoding="utf-8"))["status"],
            "failed",
        )

    def test_proxy_is_explicit_and_environment_is_disabled(self):
        captured = {}

        def handler(request):
            return httpx.Response(
                200,
                text='<a href="f.fb2.000051-000060.zip">archive</a>',
                request=request,
            )

        updater = FlibustaUpdater(
            self.config(proxy_url="socks5h://127.0.0.1:1080"),
            client_factory=self.client_factory(handler, captured),
        )
        updater.run(dry_run=True)

        self.assertEqual(captured["proxy"], "socks5h://127.0.0.1:1080")
        self.assertFalse(captured["trust_env"])

    def test_invalid_proxy_scheme_is_rejected(self):
        updater = FlibustaUpdater(self.config(proxy_url="ftp://proxy.example:21"))
        with self.assertRaisesRegex(UpdaterError, "scheme"):
            updater.run(dry_run=True)

    def test_empty_index_is_an_error_not_up_to_date(self):
        def handler(request):
            return httpx.Response(200, text='<a href="../">parent</a>', request=request)

        updater = FlibustaUpdater(
            self.config(),
            client_factory=self.client_factory(handler),
        )
        with self.assertRaisesRegex(UpdaterError, "no matching"):
            updater.run(dry_run=True)

    def test_lock_contention_prevents_parallel_updater(self):
        new = "f.fb2.000061-000070.zip"

        def handler(request):
            return httpx.Response(
                200, text=f'<a href="{new}">archive</a>', request=request
            )

        updater = FlibustaUpdater(
            self.config(), client_factory=self.client_factory(handler)
        )
        with NonBlockingFileLock(self.lock, label="test updater"):
            with self.assertRaises(UpdaterAlreadyRunning):
                updater.run(dry_run=True)

    def test_destination_must_be_inside_library_root(self):
        outside = self.root / "outside"
        updater = FlibustaUpdater(
            self.config(destination=outside, library_root=self.destination)
        )
        with self.assertRaisesRegex(UpdaterError, "inside library root"):
            updater.run(dry_run=True)

    def test_file_size_limit_is_enforced_without_publication(self):
        new = "f.fb2.000071-000080.zip"
        payload = zip_bytes(content=b"x" * 2048)

        def handler(request):
            if request.url.path.endswith("/daily/"):
                return httpx.Response(
                    200, text=f'<a href="{new}">archive</a>', request=request
                )
            return httpx.Response(200, content=payload, request=request)

        updater = FlibustaUpdater(
            self.config(max_file_size_mb=0),
            client_factory=self.client_factory(handler),
        )
        with self.assertRaisesRegex(UpdaterError, "size limits"):
            updater.run()
        self.assertFalse((self.destination / new).exists())


class ProxyHelpersTests(unittest.TestCase):
    def test_supported_proxy_schemes(self):
        for proxy in (
            "http://127.0.0.1:3128",
            "https://proxy.example:443",
            "socks5://127.0.0.1:1080",
            "socks5h://127.0.0.1:1080",
        ):
            self.assertEqual(validate_proxy_url(proxy), proxy)

    def test_invalid_proxy_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_proxy_url("ftp://proxy.example")
        with self.assertRaises(ValueError):
            validate_proxy_url("socks5://")

    def test_telegram_request_uses_proxy_and_ignores_environment(self):
        request = build_telegram_request("http://127.0.0.1:3128")
        self.assertEqual(
            str(request._client_kwargs["proxy"]), "http://127.0.0.1:3128"
        )
        self.assertFalse(request._client_kwargs["trust_env"])

        direct = build_telegram_request("")
        self.assertIsNone(direct._client_kwargs["proxy"])
        self.assertFalse(direct._client_kwargs["trust_env"])


if __name__ == "__main__":
    unittest.main()
