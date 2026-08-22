from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .bridge import JsonBridge
from .service import MAX_BODY_CHARS, Oma2FAService
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

    bridge = subparsers.add_parser("bridge", help="run the Quickshell JSON-lines bridge")
    bridge.add_argument("--no-blueferry", action="store_true", help="disable BlueFerry ingestion")
    bridge.add_argument("--webhook", action="store_true", help="enable the authenticated webhook")
    bridge.add_argument("--webhook-bind", help="webhook listen address")
    bridge.add_argument("--webhook-port", type=int, help="webhook listen port")
    bridge.add_argument(
        "--webhook-token-file",
        help="mode-0600 bearer-token file (the token itself is never an argument)",
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


def _run(args: argparse.Namespace) -> int:
    store = RuntimeStore(args.runtime_dir)
    service = Oma2FAService(store)

    if args.command == "bridge":
        config = _webhook_config(args, standalone=False)
        JsonBridge(
            service,
            enable_blueferry=not args.no_blueferry,
            webhook_config=config,
        ).serve()
        return 0
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
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return 0
    raise ValueError("unsupported command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (StoreError, ValueError, WebhookConfigError) as error:
        parser.exit(2, f"oma2fa: {error}\n")
    except BrokenPipeError:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
