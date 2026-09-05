from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .blip import (
    HOOK_ENV_EVENT,
    HOOK_ENV_HANDLE,
    HOOK_ENV_ID,
    HOOK_ENV_NAME,
    HOOK_ENV_TS,
    HOOK_EVENT_MESSAGE,
)
from .bridge import JsonBridge
from .notification import NewCodeNotifier
from .service import MAX_BODY_CHARS, MAX_SENDER_CHARS, Oma2FAService
from .settings import SettingsError, SourceSettings
from .sources import DEFAULT_SOURCE_ENABLED
from .store import RuntimeStore, StoreError
from .webhook import WebhookConfig, WebhookConfigError, WebhookServer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oma2fa",
        description="Detect and use short-lived two-factor codes locally.",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="private runtime directory (default: $XDG_RUNTIME_DIR/oma2fa)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    source_names = sorted(DEFAULT_SOURCE_ENABLED)
    bridge = subparsers.add_parser("bridge", help="run the Quickshell JSON-lines bridge")
    bridge.add_argument(
        "--disable-source",
        action="append",
        default=[],
        metavar="NAME",
        choices=source_names,
        help="pin a message source off for this run (overrides sources.json)",
    )
    bridge.add_argument(
        "--enable-source",
        action="append",
        default=[],
        metavar="NAME",
        choices=source_names,
        help="pin a message source on for this run (overrides sources.json)",
    )
    bridge.add_argument(
        "--no-blueferry",
        action="store_true",
        help="deprecated alias for --disable-source blueferry",
    )
    bridge.add_argument("--webhook", action="store_true", help="enable the authenticated webhook")
    bridge.add_argument("--webhook-bind", help="webhook listen address")
    bridge.add_argument("--webhook-port", type=int, help="webhook listen port")
    bridge.add_argument(
        "--webhook-token-file",
        help="mode-0600 bearer-token file (the token itself is never an argument)",
    )

    sources = subparsers.add_parser(
        "sources",
        help="list or change which message sources are enabled",
        description=(
            "Print each source's enabled flag. --enable/--disable persist to "
            "sources.json; a running bridge applies the change within seconds."
        ),
    )
    sources.add_argument(
        "--enable", action="append", default=[], metavar="NAME", choices=source_names
    )
    sources.add_argument(
        "--disable", action="append", default=[], metavar="NAME", choices=source_names
    )

    subparsers.add_parser("status", help="print local backend status as JSON")
    subparsers.add_parser("list", help="print unexpired derived records as JSON")

    ingest = subparsers.add_parser(
        "ingest",
        help="detect a code in a message body read from stdin",
        description=(
            "Read one message body from stdin, detect a code locally, "
            "and store only the derived record."
        ),
    )
    ingest.add_argument("--sender", default="", help="sender/service hint")
    ingest.add_argument("--source", default="manual", help="transport label")
    ingest.add_argument("--timestamp", help="ISO timestamp or epoch seconds")
    ingest.add_argument("--message-id", help="transport-stable message identifier")

    subparsers.add_parser(
        "blip-hook",
        help="Blip message_hook entry point: body on stdin, BLIP_HOOK_* in the environment",
        description=(
            "Invoked by Blip's collector for each new inbound iMessage/SMS when "
            "bridge.conf sets message_hook=<...>/bin/oma2fa-blip-hook. Honours the "
            "blip source toggle and never fails the caller."
        ),
    )

    delete = subparsers.add_parser("delete", help="delete one record")
    delete.add_argument("record_id")
    subparsers.add_parser("clear", help="delete all current records")

    webhook = subparsers.add_parser("webhook", help="run only the authenticated phone webhook")
    webhook.add_argument("--bind", help="listen address")
    webhook.add_argument("--port", type=int, help="listen port")
    webhook.add_argument(
        "--token-file",
        help="mode-0600 bearer-token file (the token itself is never an argument)",
    )
    return parser


def _print(value: object) -> None:
    json.dump(value, sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")


def _webhook_config(args: argparse.Namespace, *, standalone: bool) -> WebhookConfig:
    return WebhookConfig.from_env(
        force_enabled=standalone or bool(getattr(args, "webhook", False)),
        bind=getattr(args, "bind", None) or getattr(args, "webhook_bind", None),
        port=getattr(args, "port", None) or getattr(args, "webhook_port", None),
        token_file=(getattr(args, "token_file", None) or getattr(args, "webhook_token_file", None)),
    )


def _read_body() -> str:
    body = sys.stdin.read(MAX_BODY_CHARS + 1)
    if len(body) > MAX_BODY_CHARS:
        raise ValueError("message body exceeds the size limit")
    return body.rstrip("\r\n")


def _run_blip_hook(service: Oma2FAService, environ: Mapping[str, str]) -> int:
    """Reduce one Blip message to a record. Quiet and zero on every failure."""

    if environ.get(HOOK_ENV_EVENT, "") != HOOK_EVENT_MESSAGE:
        return 0
    if not SourceSettings(defaults=DEFAULT_SOURCE_ENABLED).enabled("blip"):
        return 0
    try:
        body = _read_body()
    except (OSError, UnicodeError, ValueError):
        return 0
    if not body:
        return 0
    # Blip resolves contact names on the Mac, so a name is trusted the way
    # BlueFerry's contact names are; otherwise the raw handle is the sender.
    name = environ.get(HOOK_ENV_NAME, "").strip()
    handle = environ.get(HOOK_ENV_HANDLE, "").strip()
    sender = (name or handle)[:MAX_SENDER_CHARS]
    message_id = environ.get(HOOK_ENV_ID, "").strip() or None
    timestamp = environ.get(HOOK_ENV_TS, "").strip() or None
    try:
        result = service.ingest(
            sender=sender,
            body=body,
            source="blip",
            timestamp=timestamp,
            message_id=message_id,
        )
    except ValueError:
        return 0
    finally:
        del body
    # Blip discards stdout; keep it free of the record anyway.
    _print({"accepted": result.accepted, "reason": result.reason})
    return 0


def _run(args: argparse.Namespace) -> int:
    if args.command == "sources":
        settings = SourceSettings(defaults=DEFAULT_SOURCE_ENABLED)
        for name in args.enable:
            settings.set_enabled(name, True)
        for name in args.disable:
            settings.set_enabled(name, False)
        _print(
            {
                "path": str(settings.path),
                "sources": {name: {"enabled": flag} for name, flag in settings.snapshot().items()},
            }
        )
        return 0

    store = RuntimeStore(args.runtime_dir)
    notifier = NewCodeNotifier()
    service = Oma2FAService(store, on_code=notifier.notify)

    if args.command == "bridge":
        config = _webhook_config(args, standalone=False)
        overrides: dict[str, bool] = dict.fromkeys(args.enable_source, True)
        overrides.update(dict.fromkeys(args.disable_source, False))
        if args.no_blueferry:
            overrides["blueferry"] = False
        JsonBridge(
            service,
            source_overrides=overrides,
            webhook_config=config,
        ).serve()
        return 0
    if args.command == "blip-hook":
        return _run_blip_hook(service, os.environ)
    if args.command == "status":
        _print(service.status())
        return 0
    if args.command == "list":
        _print(service.snapshot())
        return 0
    if args.command == "ingest":
        ingest_result = service.ingest(
            sender=args.sender,
            body=_read_body(),
            source=args.source,
            timestamp=args.timestamp,
            message_id=args.message_id,
        )
        _print(ingest_result.public_dict())
        return 0
    if args.command == "delete":
        _print({"record_id": args.record_id, "deleted": service.delete(args.record_id)})
        return 0
    if args.command == "clear":
        _print({"cleared": service.clear()})
        return 0
    if args.command == "webhook":
        config = _webhook_config(args, standalone=True)
        server = WebhookServer(service, config)
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def stop_for_sigterm(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop_for_sigterm)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
            signal.signal(signal.SIGTERM, previous_sigterm)
        return 0
    raise ValueError("unsupported command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (SettingsError, StoreError, ValueError, WebhookConfigError) as error:
        parser.exit(2, f"oma2fa: {error}\n")
    except BrokenPipeError:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
