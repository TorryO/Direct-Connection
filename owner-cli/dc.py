#!/usr/bin/env python3
"""Direct-Connection owner CLI.

Uses only the standard library so it runs anywhere Python 3.9+ is installed.

Environment:
    DC_SERVER_URL   base URL of the server  (default http://127.0.0.1:8000)
    DC_ADMIN_KEY    owner admin key, same value the server uses

Examples:
    python owner-cli/dc.py provision /media/FLASH --name "Sam's stick"
    python owner-cli/dc.py send <drive-id> ./photos/*.jpg --note "Trip photos"
    python owner-cli/dc.py files <drive-id>
    python owner-cli/dc.py revoke <drive-id>
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "http://127.0.0.1:8000"
AGENT_SOURCE = Path(__file__).resolve().parent.parent / "agent" / "agent.py"
CONFIG_NAME = "direct-connection.json"


class CliError(RuntimeError):
    pass


def _server_url() -> str:
    return os.environ.get("DC_SERVER_URL", DEFAULT_SERVER).rstrip("/")


def _admin_key() -> str:
    key = os.environ.get("DC_ADMIN_KEY", "").strip()
    if not key:
        raise CliError("DC_ADMIN_KEY is not set; export it with the server's admin key.")
    return key


def _request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    url = _server_url() + path
    headers = {"X-Admin-Key": _admin_key()}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (ValueError, AttributeError):
            pass
        raise CliError(f"{method} {path} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise CliError(f"Cannot reach {url}: {exc.reason}") from exc
    if not raw:
        return None
    return json.loads(raw)


def _multipart(
    filename: str, content: bytes, fields: dict[str, str]
) -> tuple[bytes, str]:
    boundary = "----directconnection" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
            f"\r\n{value}\r\n".encode("utf-8")
        )
    guessed = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe_name = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{safe_name}"\r\nContent-Type: {guessed}\r\n\r\n'.encode("utf-8")
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# -- commands -----------------------------------------------------------


def cmd_create(args: argparse.Namespace) -> int:
    drive = _request("POST", "/api/drives", body=json.dumps(
        {"name": args.name}).encode("utf-8"), content_type="application/json")
    print(f"Drive created: {drive['name']}")
    print(f"  id:    {drive['id']}")
    print(f"  token: {drive['token']}")
    print("\nThis token is shown once. Put it on the drive with:")
    print(f"  DC_SERVER_URL={_server_url()} python owner-cli/dc.py provision <drive-letter> "
          f"--drive-id {drive['id']} --token {drive['token']}")
    return 0


def cmd_drives(args: argparse.Namespace) -> int:
    drives = _request("GET", "/api/drives")
    if not drives:
        print("No drives registered yet.")
        return 0
    for drive in drives:
        state = "REVOKED" if drive["revoked"] else "active"
        last_seen = drive.get("last_seen_at") or "never"
        print(
            f"{drive['id']}  {drive['name']!r}  {state}  "
            f"pending={drive['pending_count']}  last_seen={last_seen}"
        )
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files]
    for path in paths:
        if not path.is_file():
            raise CliError(f"Not a file: {path}")
    failures = 0
    for path in paths:
        content = path.read_bytes()
        body, content_type = _multipart(
            path.name, content, {"note": args.note or ""}
        )
        try:
            record = _request(
                "POST",
                f"/api/drives/{args.drive_id}/files",
                body=body,
                content_type=content_type,
            )
        except CliError as exc:
            failures += 1
            print(f"  failed  {path.name}: {exc}", file=sys.stderr)
            continue
        print(f"  queued  {record['filename']} ({_human(record['size'])}) "
              f"sha256={record['sha256'][:12]}…")
    if failures:
        print(f"{failures} of {len(paths)} upload(s) failed.", file=sys.stderr)
        return 1
    print(f"{len(paths)} file(s) queued for drive {args.drive_id}. "
          "They arrive the next time the recipient opens the agent.")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    files = _request("GET", f"/api/drives/{args.drive_id}/files")
    if not files:
        print("Nothing queued for this drive.")
        return 0
    for file in files:
        when = file["delivered_at"] or "pending"
        print(
            f"{file['id']}  {file['filename']}  {_human(file['size'])}  {when}"
            + (f"  note: {file['note']}" if file["note"] else "")
        )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    _request("POST", f"/api/drives/{args.drive_id}/revoke")
    print(f"Drive {args.drive_id} revoked. Its agent can no longer download anything.")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    drive = _request("POST", f"/api/drives/{args.drive_id}/rotate-token")
    print(f"New token for {drive['name']} (old token is now dead):")
    print(f"  {drive['token']}")
    print("Update direct-connection.json on the drive with it.")
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    target = Path(args.directory).expanduser()
    if not target.is_dir():
        raise CliError(f"Not a directory: {target}")

    drive_id = args.drive_id
    token = args.token
    if not drive_id or not token:
        drive = _request("POST", "/api/drives", body=json.dumps(
            {"name": args.name or target.name}).encode("utf-8"),
            content_type="application/json")
        drive_id, token = drive["id"], drive["token"]
        print(f"Registered new drive {drive_id} ({drive['name']}).")

    config = {
        "server_url": _server_url(),
        "drive_id": drive_id,
        "token": token,
        "inbox": "Inbox",
        "poll_seconds": 30,
    }
    (target / CONFIG_NAME).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (target / "Inbox").mkdir(exist_ok=True)
    if AGENT_SOURCE.is_file():
        shutil.copy2(AGENT_SOURCE, target / "agent.py")
    print(f"Prepared {target}: {CONFIG_NAME}, agent.py and Inbox/")

    # Tell the server the program is on the drive so the dashboard shows it.
    try:
        _request("POST", f"/api/drives/{drive_id}/installed")
    except CliError as exc:
        print(f"  warning: could not report the install to the server: {exc}",
              file=sys.stderr)

    print("The recipient starts agent.py themselves; nothing runs automatically.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dc", description="Owner tool for Direct-Connection."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="register a new drive and get its token")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("drives", help="list registered drives")
    p.set_defaults(func=cmd_drives)

    p = sub.add_parser("send", help="queue files for a drive")
    p.add_argument("drive_id")
    p.add_argument("files", nargs="+")
    p.add_argument("--note", default=None, help="short message shown next to the files")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("files", help="show a drive's files and delivery status")
    p.add_argument("drive_id")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("revoke", help="disable a drive's token permanently")
    p.add_argument("drive_id")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("rotate", help="replace a drive's token")
    p.add_argument("drive_id")
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser(
        "provision", help="write config + agent onto a drive; registers a new drive unless "
        "--drive-id and --token are given"
    )
    p.add_argument("directory")
    p.add_argument("--name", default=None)
    p.add_argument("--drive-id", default=None)
    p.add_argument("--token", default=None)
    p.set_defaults(func=cmd_provision)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
