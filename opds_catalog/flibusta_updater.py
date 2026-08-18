"""Native, transactional downloader for Flibusta daily FB2 archives."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx

from opds_catalog.network import validate_proxy_url
from opds_catalog.process_lock import LockBusy, NonBlockingFileLock


class UpdaterError(RuntimeError):
    """Base updater error."""


class UpdaterAlreadyRunning(UpdaterError):
    """Raised when another updater process owns the lock."""


class ArchiveValidationError(UpdaterError):
    """Raised when a downloaded ZIP archive is invalid."""


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


@dataclass(frozen=True)
class UpdateConfig:
    source_url: str
    destination: Path
    library_root: Path | None = None
    file_pattern: str = "*.fb2.*.zip"
    proxy_url: str = ""
    timeout_seconds: float = 120.0
    retries: int = 3
    validate_zip: bool = True
    max_file_size_mb: int = 2048
    max_total_size_mb: int = 10240
    min_free_space_mb: int = 2048
    state_file: Path = Path("flibusta-update-state.json")
    lock_file: Path = Path("flibusta-update.lock")
    user_agent: str = "SOPDS-Flibusta-Updater/1.0"


@dataclass
class UpdateResult:
    discovered: int = 0
    existing: int = 0
    downloaded: int = 0
    downloaded_bytes: int = 0
    files: list[str] = field(default_factory=list)
    dry_run: bool = False


class FlibustaUpdater:
    """Discover, stage, validate, and atomically publish daily archives."""

    def __init__(
        self,
        config: UpdateConfig,
        logger: logging.Logger | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.client_factory = client_factory

    def run(self, dry_run: bool = False) -> UpdateResult:
        self._validate_config()
        try:
            with NonBlockingFileLock(
                self.config.lock_file, label="Flibusta updater"
            ):
                return self._run_locked(dry_run=dry_run)
        except LockBusy as exc:
            raise UpdaterAlreadyRunning(str(exc)) from exc

    def _run_locked(self, dry_run: bool) -> UpdateResult:
        self.config.destination.mkdir(parents=True, exist_ok=True)
        client_kwargs = {
            "follow_redirects": True,
            "timeout": httpx.Timeout(self.config.timeout_seconds),
            "headers": {"User-Agent": self.config.user_agent},
            "trust_env": False,
        }
        proxy = validate_proxy_url(
            self.config.proxy_url, service="Flibusta"
        )
        if proxy:
            client_kwargs["proxy"] = proxy

        with self.client_factory(**client_kwargs) as client:
            urls = self._discover_with_retry(client)
            result = UpdateResult(discovered=len(urls), dry_run=dry_run)
            pending = []
            for url in urls:
                filename = self._filename(url)
                if (self.config.destination / filename).is_file():
                    result.existing += 1
                else:
                    pending.append((url, filename))

            if dry_run or not pending:
                result.files = [filename for _, filename in pending]
                self._write_state(result, status="dry-run" if dry_run else "up-to-date")
                return result

            staging_parent = self.config.destination / ".sopds-update-staging"
            staging_parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix="run-", dir=staging_parent))
            staged: list[tuple[Path, str, int]] = []
            try:
                for url, filename in pending:
                    self._ensure_free_space(staging)
                    staged_path, size = self._download_with_retry(client, url, staging, filename)
                    staged.append((staged_path, filename, size))
                    staged_total = sum(item_size for _, _, item_size in staged)
                    if staged_total > self.config.max_total_size_mb * 1024 * 1024:
                        raise UpdaterError(
                            "update exceeds max_total_size_mb="
                            f"{self.config.max_total_size_mb}"
                        )

                # Publish only after every archive has downloaded and validated.
                conflicts = [
                    self.config.destination / filename
                    for _, filename, _ in staged
                    if (self.config.destination / filename).exists()
                ]
                if conflicts:
                    raise UpdaterError(
                        "destination appeared during update: "
                        + ", ".join(str(path) for path in conflicts)
                    )
                for staged_path, filename, size in staged:
                    destination = self.config.destination / filename
                    os.replace(staged_path, destination)
                    result.downloaded += 1
                    result.downloaded_bytes += size
                    result.files.append(filename)

                self._fsync_directory(self.config.destination)
                self._write_state(result, status="success")
                return result
            except Exception as exc:
                self._write_state(result, status="failed", error=str(exc))
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def _discover_with_retry(self, client: httpx.Client) -> list[str]:
        last_error = None
        for attempt in range(1, self.config.retries + 1):
            try:
                urls = self._discover(client)
                if not urls:
                    raise UpdaterError(
                        "Flibusta index contained no matching archive links"
                    )
                return urls
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.retries:
                    break
                delay = min(2 ** (attempt - 1), 8)
                self.logger.warning(
                    "index discovery failed (%s/%s): %s; retry in %ss",
                    attempt,
                    self.config.retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise UpdaterError(f"failed to read Flibusta index: {last_error}") from last_error

    def _discover(self, client: httpx.Client) -> list[str]:
        response = client.get(self.config.source_url)
        response.raise_for_status()
        parser = _LinkParser()
        parser.feed(response.text)

        discovered: dict[str, str] = {}
        source_host = urlparse(str(response.url)).hostname
        for href in parser.links:
            url = urljoin(str(response.url), href)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.hostname != source_host:
                continue
            try:
                filename = self._filename(url)
            except UpdaterError:
                continue
            if not fnmatch.fnmatch(filename.lower(), self.config.file_pattern.lower()):
                continue
            discovered[filename] = url
        return [discovered[name] for name in sorted(discovered)]

    def _download_with_retry(
        self,
        client: httpx.Client,
        url: str,
        staging: Path,
        filename: str,
    ) -> tuple[Path, int]:
        last_error = None
        for attempt in range(1, self.config.retries + 1):
            part = staging / f"{filename}.part"
            try:
                size = self._download(client, url, part)
                if self.config.validate_zip:
                    self._validate_zip(part)
                final_staged = staging / filename
                os.replace(part, final_staged)
                return final_staged, size
            except Exception as exc:
                last_error = exc
                part.unlink(missing_ok=True)
                if attempt >= self.config.retries:
                    break
                delay = min(2 ** (attempt - 1), 8)
                self.logger.warning(
                    "download failed (%s/%s) for %s: %s; retry in %ss",
                    attempt,
                    self.config.retries,
                    filename,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise UpdaterError(f"failed to download {filename}: {last_error}") from last_error

    def _download(self, client: httpx.Client, url: str, part: Path) -> int:
        total = 0
        max_bytes = self.config.max_file_size_mb * 1024 * 1024
        with client.stream("GET", url) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise UpdaterError(
                    f"archive exceeds max_file_size_mb={self.config.max_file_size_mb}"
                )
            with part.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise UpdaterError(
                            f"archive exceeds max_file_size_mb={self.config.max_file_size_mb}"
                        )
                output.flush()
                os.fsync(output.fileno())
        if total == 0:
            raise UpdaterError(f"empty response for {url}")
        return total

    def _ensure_free_space(self, path: Path):
        free = shutil.disk_usage(path).free
        minimum = self.config.min_free_space_mb * 1024 * 1024
        if free < minimum:
            raise UpdaterError(
                "insufficient free space: "
                f"{free // (1024 * 1024)} MiB available, "
                f"{self.config.min_free_space_mb} MiB required"
            )

    @staticmethod
    def _validate_zip(path: Path):
        try:
            with zipfile.ZipFile(path) as archive:
                if not archive.namelist():
                    raise ArchiveValidationError(f"empty ZIP archive: {path.name}")
                corrupt = archive.testzip()
                if corrupt is not None:
                    raise ArchiveValidationError(
                        f"CRC failure in {path.name}: {corrupt}"
                    )
        except (zipfile.BadZipFile, OSError) as exc:
            raise ArchiveValidationError(f"invalid ZIP archive {path.name}: {exc}") from exc

    @staticmethod
    def _filename(url: str) -> str:
        filename = Path(unquote(urlparse(url).path)).name
        if not filename or filename in {".", ".."}:
            raise UpdaterError(f"URL has no safe filename: {url}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
            raise UpdaterError(f"URL contains an unsafe filename: {url}")
        return filename

    def _write_state(self, result: UpdateResult, status: str, error: str = ""):
        state = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "source_url": self.config.source_url,
            "result": asdict(result),
        }
        if error:
            state["error"] = error
        self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config.state_file.with_suffix(
            self.config.state_file.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.config.state_file)

    def _validate_config(self):
        source = urlparse(self.config.source_url)
        if source.scheme not in {"http", "https"} or not source.netloc:
            raise UpdaterError("source_url must be an absolute HTTP(S) URL")
        if self.config.retries < 1:
            raise UpdaterError("retries must be >= 1")
        if self.config.timeout_seconds <= 0:
            raise UpdaterError("timeout_seconds must be > 0")
        if self.config.max_file_size_mb <= 0 or self.config.max_total_size_mb <= 0:
            raise UpdaterError("download size limits must be > 0")
        if self.config.min_free_space_mb < 0:
            raise UpdaterError("min_free_space_mb must be >= 0")
        if self.config.library_root is not None:
            root = self.config.library_root.resolve()
            destination = self.config.destination.resolve()
            if not destination.is_relative_to(root):
                raise UpdaterError(
                    f"destination {destination} must be inside library root {root}"
                )
        try:
            validate_proxy_url(self.config.proxy_url, service="Flibusta")
        except ValueError as exc:
            raise UpdaterError(str(exc)) from exc

    @staticmethod
    def _fsync_directory(path: Path):
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Directory fsync is not available on every supported filesystem.
            pass
