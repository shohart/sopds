"""Manage scheduled Flibusta daily archive updates."""

import logging
import os
import signal
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from constance import config
from django.conf import settings as main_settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from opds_catalog import settings
from opds_catalog.flibusta_updater import (
    FlibustaUpdater,
    UpdateConfig,
    UpdaterAlreadyRunning,
    UpdaterError,
)


class Command(BaseCommand):
    help = "Download new Flibusta daily archives and optionally rescan the library."

    def add_arguments(self, parser):
        parser.add_argument(
            "command", choices=("once", "start", "stop", "restart", "status")
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Discover missing archives without downloading or scanning.",
        )
        parser.add_argument(
            "--no-scan",
            action="store_true",
            help="Do not launch the SOPDS scanner after a successful update.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run once even if SOPDS_FLIBUSTA_UPDATE_ENABLED is false.",
        )
        parser.add_argument("--verbose", action="store_true")

    def handle(self, *args, **options):
        self.logger = self._configure_logger(options["verbose"])
        self.pidfile = Path(config.SOPDS_FLIBUSTA_UPDATE_PID)
        action = options["command"]

        if action == "once":
            return self.run_once(
                dry_run=options["dry_run"],
                no_scan=options["no_scan"],
                force=options["force"],
            )
        if action == "start":
            return self.start()
        if action == "stop":
            return self.stop()
        if action == "restart":
            self.stop(ignore_missing=True)
            return self.start()
        if action == "status":
            return self.status()

    def run_once(self, dry_run=False, no_scan=False, force=False):
        if not config.SOPDS_FLIBUSTA_UPDATE_ENABLED and not force:
            self.stdout.write("Flibusta updater is disabled by configuration.")
            return None

        updater = FlibustaUpdater(self._updater_config(), logger=self.logger)
        try:
            result = updater.run(dry_run=dry_run)
        except UpdaterAlreadyRunning as exc:
            self.logger.warning("Flibusta update skipped: %s", exc)
            self.stdout.write(self.style.WARNING(str(exc)))
            return None
        except UpdaterError as exc:
            self.logger.error("Flibusta update failed: %s", exc)
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            "Flibusta update: discovered=%d existing=%d downloaded=%d bytes=%d%s"
            % (
                result.discovered,
                result.existing,
                result.downloaded,
                result.downloaded_bytes,
                " (dry-run)" if result.dry_run else "",
            )
        )

        should_scan = (
            result.downloaded > 0
            and not dry_run
            and not no_scan
            and config.SOPDS_FLIBUSTA_SCAN_AFTER_UPDATE
        )
        if should_scan:
            self.stdout.write("New archives published; starting SOPDS scan.")
            call_command("sopds_scanner", "scan")
        elif result.downloaded == 0:
            self.stdout.write("No new archives; scan is not required.")
        return result

    def start(self):
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(str(os.getpid()), encoding="ascii")
        self.scheduler = BlockingScheduler()
        self._install_update_job()
        self.scheduler.add_job(
            self._check_settings,
            "interval",
            minutes=10,
            id="settings-check",
            replace_existing=True,
        )
        self.stdout.write(
            "Flibusta updater scheduler started: %s"
            % self._schedule_description()
        )
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.pidfile.unlink(missing_ok=True)

    def _scheduled_run(self):
        try:
            self.run_once()
        except Exception:
            self.logger.exception("Scheduled Flibusta update failed")

    def _install_update_job(self):
        self._schedule = self._schedule_signature()
        minute, hour, day, day_of_week = self._schedule
        self.scheduler.add_job(
            self._scheduled_run,
            "cron",
            minute=minute,
            hour=hour,
            day=day,
            day_of_week=day_of_week,
            id="flibusta-update",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    def _check_settings(self):
        current = self._schedule_signature()
        if current != self._schedule:
            self.logger.info("Flibusta update schedule changed: %s", current)
            self._install_update_job()

    def stop(self, ignore_missing=False):
        try:
            pid = int(self.pidfile.read_text(encoding="ascii").strip())
        except (FileNotFoundError, ValueError):
            if ignore_missing:
                return
            raise CommandError("Flibusta updater PID file not found or invalid")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            if not ignore_missing:
                raise CommandError(f"Flibusta updater process {pid} not found")
        finally:
            self.pidfile.unlink(missing_ok=True)

    def status(self):
        try:
            pid = int(self.pidfile.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            self.stdout.write("Flibusta updater scheduler: stopped")
            return False
        self.stdout.write(f"Flibusta updater scheduler: running (pid={pid})")
        return True

    def _updater_config(self):
        destination = str(config.SOPDS_FLIBUSTA_DESTINATION).strip()
        if not destination:
            destination = str(config.SOPDS_ROOT_LIB)
        destination_path = Path(destination).resolve()
        control_dir = destination_path / ".sopds-control"
        state_value = str(config.SOPDS_FLIBUSTA_UPDATE_STATE).strip()
        lock_value = str(config.SOPDS_FLIBUSTA_UPDATE_LOCK).strip()
        return UpdateConfig(
            source_url=str(config.SOPDS_FLIBUSTA_SOURCE_URL).strip(),
            destination=destination_path,
            library_root=Path(str(config.SOPDS_ROOT_LIB)).resolve(),
            file_pattern=str(config.SOPDS_FLIBUSTA_FILE_PATTERN).strip(),
            proxy_url=str(config.SOPDS_FLIBUSTA_PROXY_URL).strip(),
            timeout_seconds=float(config.SOPDS_FLIBUSTA_TIMEOUT_SECONDS),
            retries=int(config.SOPDS_FLIBUSTA_RETRIES),
            validate_zip=bool(config.SOPDS_FLIBUSTA_VALIDATE_ZIP),
            max_file_size_mb=int(config.SOPDS_FLIBUSTA_MAX_FILE_SIZE_MB),
            max_total_size_mb=int(config.SOPDS_FLIBUSTA_MAX_TOTAL_SIZE_MB),
            min_free_space_mb=int(config.SOPDS_FLIBUSTA_MIN_FREE_SPACE_MB),
            state_file=Path(state_value) if state_value else control_dir / "flibusta-update-state.json",
            lock_file=Path(lock_value) if lock_value else control_dir / "flibusta-update.lock",
        )

    @staticmethod
    def _schedule_signature():
        return (
            str(config.SOPDS_FLIBUSTA_SHED_MIN),
            str(config.SOPDS_FLIBUSTA_SHED_HOUR),
            str(config.SOPDS_FLIBUSTA_SHED_DAY),
            str(config.SOPDS_FLIBUSTA_SHED_DOW),
        )

    def _schedule_description(self):
        minute, hour, day, day_of_week = self._schedule_signature()
        return f"minute={minute}, hour={hour}, day={day}, day_of_week={day_of_week}"

    def _configure_logger(self, verbose):
        logger = logging.getLogger("sopds.flibusta_updater")
        logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")

        log_path = Path(config.SOPDS_FLIBUSTA_UPDATE_LOG)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not any(
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename) == log_path.resolve()
            for handler in logger.handlers
        ):
            file_handler = logging.FileHandler(log_path)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        if verbose and not any(
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            for handler in logger.handlers
        ):
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)
        return logger
