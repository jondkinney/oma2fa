from __future__ import annotations

import ipaddress
import os
import secrets
import shlex
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .util import atomic_write, ensure_directory
from .util import config_root as default_config_root
from .webhook import DEFAULT_WEBHOOK_PORT, WebhookConfig, WebhookConfigError

SERVICE_NAME = "oma2fa-webhook.service"
_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_SHORTCUT_COPY_VALUES = {
    "shortcut_name": "Send to Oma2FA",
    "trigger_phrase": "code",
    "authorization_header": "Authorization",
    "content_type_header": "Content-Type",
    "content_type_value": "application/json",
    "sender_key": "sender",
    "sender_value": "SMS",
    "body_key": "body",
    "source_key": "source",
    "source_value": "ios-shortcuts",
}


class WebhookSetupError(RuntimeError):
    pass


def _decode_environment_value(value: str) -> str:
    try:
        words = shlex.split(value, comments=False, posix=True)
    except ValueError:
        return ""
    return words[0] if len(words) == 1 else ""


def _environment_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class WebhookManager:
    """Provision and control the standalone phone webhook without exposing its token."""

    def __init__(
        self,
        *,
        copy_secret: Callable[[str], None],
        environ: Mapping[str, str] = os.environ,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        config_root: Path | None = None,
        unit_source: Path | None = None,
    ) -> None:
        self._copy_secret = copy_secret
        self._environ = environ
        self._run = run
        self._which = which
        root = config_root or default_config_root(environ)
        self.settings_dir = root / "oma2fa"
        self.environment_path = self.settings_dir / "webhook.env"
        self.token_path = self.settings_dir / "webhook-token"
        self.user_unit_dir = root / "systemd" / "user"
        self.unit_path = self.user_unit_dir / SERVICE_NAME
        self.unit_source = unit_source or (
            Path(__file__).resolve().parents[1] / "systemd" / SERVICE_NAME
        )

    def _run_command(self, command: list[str], *, timeout: float = 8) -> bool:
        try:
            result = self._run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _systemctl(self, *arguments: str) -> bool:
        executable = self._which("systemctl")
        if not executable:
            return False
        return self._run_command([executable, "--user", *arguments])

    def _service_state(self) -> tuple[bool, bool]:
        if not self.unit_path.is_file() or self.unit_path.is_symlink():
            return False, False
        return (
            self._systemctl("is-enabled", "--quiet", SERVICE_NAME),
            self._systemctl("is-active", "--quiet", SERVICE_NAME),
        )

    def _tailscale_ip(self) -> str:
        executable = self._which("tailscale")
        if not executable:
            return ""
        try:
            result = self._run(
                [executable, "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode != 0 or len(result.stdout) > 256:
                return ""
            candidate = result.stdout.strip().splitlines()[0].strip()
            address = ipaddress.ip_address(candidate)
            return str(address) if address in _TAILSCALE_NETWORK else ""
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return ""

    def _read_environment(self) -> dict[str, str]:
        if not self.environment_path.is_file() or self.environment_path.is_symlink():
            return {}
        try:
            info = self.environment_path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                return {}
            raw = self.environment_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return {}
        if len(raw) > 16_384:
            return {}
        values: dict[str, str] = {}
        allowed = {
            "OMA2FA_WEBHOOK_BIND",
            "OMA2FA_WEBHOOK_PORT",
            "OMA2FA_WEBHOOK_TRANSPORT",
            "OMA2FA_WEBHOOK_TOKEN_FILE",
        }
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name in allowed:
                decoded = _decode_environment_value(value.strip())
                if decoded:
                    values[name] = decoded
        return values

    def _token_file_from_environment(self, values: Mapping[str, str]) -> Path | None:
        raw_path = values.get("OMA2FA_WEBHOOK_TOKEN_FILE", "")
        if not raw_path:
            return None
        path = Path(raw_path)
        return path if path.is_absolute() else None

    def _read_token(self, path: Path) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 4096
            ):
                raise WebhookSetupError("The webhook token file is not private")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                token = handle.read(4097).decode("utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise WebhookSetupError("The webhook token could not be read") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(token.encode("utf-8")) < 24:
            raise WebhookSetupError("The webhook token is invalid")
        return token

    def _configuration(self) -> tuple[WebhookConfig | None, Path | None, str]:
        values = self._read_environment()
        if not values:
            return None, None, "not configured"
        token_path = self._token_file_from_environment(values)
        if token_path is None:
            return None, None, "token file is not configured"
        try:
            token = self._read_token(token_path)
            port = int(values.get("OMA2FA_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)))
            config = WebhookConfig(
                enabled=True,
                bind=values.get("OMA2FA_WEBHOOK_BIND", "127.0.0.1"),
                port=port,
                token=token,
                transport=values.get("OMA2FA_WEBHOOK_TRANSPORT", "loopback"),
            )
            config.validate()
        except (ValueError, WebhookConfigError, WebhookSetupError):
            return None, token_path, "configuration needs attention"
        return config, token_path, "ready"

    @staticmethod
    def _endpoint(config: WebhookConfig) -> str:
        if config.bind.casefold() == "localhost":
            host = "localhost"
        else:
            address = ipaddress.ip_address(config.bind)
            host = f"[{address}]" if address.version == 6 else str(address)
        return f"http://{host}:{config.port}/v1/ingest"

    def status(self) -> dict[str, Any]:
        config, token_path, detail = self._configuration()
        enabled, running = self._service_state()
        tailscale_ip = self._tailscale_ip()
        return {
            "configured": config is not None and self.unit_path.is_file(),
            "configuration_present": self.environment_path.exists(),
            "unit_installed": self.unit_path.is_file() and not self.unit_path.is_symlink(),
            "enabled": enabled,
            "running": running,
            "bind": config.bind if config is not None else "",
            "port": config.port if config is not None else DEFAULT_WEBHOOK_PORT,
            "transport": config.transport if config is not None else "",
            "endpoint": self._endpoint(config) if config is not None else "",
            "token_present": token_path is not None and config is not None,
            "tailscale_available": bool(tailscale_ip),
            "tailscale_ip": tailscale_ip,
            "detail": detail,
        }

    @staticmethod
    def _ensure_directory(path: Path, mode: int) -> None:
        ensure_directory(path, mode, error=WebhookSetupError, label="webhook configuration")

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int) -> None:
        atomic_write(path, content, mode, error=WebhookSetupError, label="webhook configuration")

    def _install_unit(self) -> None:
        try:
            source = self.unit_source.read_bytes()
        except OSError as error:
            raise WebhookSetupError("The webhook service template is unavailable") from error
        if len(source) > 65_536:
            raise WebhookSetupError("The webhook service template is invalid")
        self._ensure_directory(self.user_unit_dir, 0o755)
        self._atomic_write(self.unit_path, source, 0o644)

    def configure_tailscale(self, port: int = DEFAULT_WEBHOOK_PORT) -> dict[str, Any]:
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65_535:
            raise WebhookSetupError("The webhook port must be between 1024 and 65535")
        tailscale_ip = self._tailscale_ip()
        if not tailscale_ip:
            raise WebhookSetupError("Connect this computer to Tailscale before setup")

        self._ensure_directory(self.settings_dir, 0o700)
        if self.token_path.exists() or self.token_path.is_symlink():
            self._read_token(self.token_path)
        else:
            self._atomic_write(self.token_path, (secrets.token_hex(32) + "\n").encode(), 0o600)

        environment = "\n".join(
            (
                f"OMA2FA_WEBHOOK_BIND={tailscale_ip}",
                "OMA2FA_WEBHOOK_TRANSPORT=vpn",
                f"OMA2FA_WEBHOOK_PORT={port}",
                "OMA2FA_WEBHOOK_TOKEN_FILE=" + _environment_value(str(self.token_path)),
                "",
            )
        )
        self._atomic_write(self.environment_path, environment.encode(), 0o600)
        was_running = self._service_state()[1]
        self._install_unit()
        if not self._systemctl("daemon-reload"):
            raise WebhookSetupError("Could not reload the user service manager")
        if not self._systemctl("enable", "--now", SERVICE_NAME):
            raise WebhookSetupError("Could not enable the phone webhook")
        if was_running and not self._systemctl("restart", SERVICE_NAME):
            raise WebhookSetupError("Could not restart the phone webhook")
        return self.status()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        config, _, _ = self._configuration()
        if config is None or not self.unit_path.is_file():
            raise WebhookSetupError("Set up the phone webhook first")
        arguments = ("enable", "--now") if enabled else ("disable", "--now")
        if not self._systemctl(*arguments, SERVICE_NAME):
            action = "enable" if enabled else "disable"
            raise WebhookSetupError(f"Could not {action} the phone webhook")
        return self.status()

    def copy_endpoint(self) -> dict[str, bool]:
        config, _, _ = self._configuration()
        if config is None:
            raise WebhookSetupError("Set up the phone webhook first")
        self._copy_secret(self._endpoint(config))
        return {"copied": True}

    def copy_token(self) -> dict[str, bool]:
        _, token_path, _ = self._configuration()
        if token_path is None:
            raise WebhookSetupError("Set up the phone webhook first")
        self._copy_secret(self._read_token(token_path))
        return {"copied": True}

    def copy_setup_field(self, field_id: str) -> dict[str, bool]:
        """Copy one allowlisted Shortcut value without returning it to QML."""

        if field_id == "webhook_url":
            return self.copy_endpoint()
        if field_id == "authorization_value":
            _, token_path, _ = self._configuration()
            if token_path is None:
                raise WebhookSetupError("Set up the phone webhook first")
            self._copy_secret(f"Bearer {self._read_token(token_path)}")
            return {"copied": True}

        value = _SHORTCUT_COPY_VALUES.get(field_id)
        if value is None:
            raise WebhookSetupError("Unknown Shortcut setup field")
        self._copy_secret(value)
        return {"copied": True}

    def rotate_token(self) -> dict[str, Any]:
        config, token_path, _ = self._configuration()
        if config is None or token_path is None:
            raise WebhookSetupError("Set up the phone webhook first")
        self._atomic_write(token_path, (secrets.token_hex(32) + "\n").encode(), 0o600)
        if self._service_state()[1] and not self._systemctl("restart", SERVICE_NAME):
            raise WebhookSetupError("The token changed, but the phone webhook could not restart")
        return self.status()
