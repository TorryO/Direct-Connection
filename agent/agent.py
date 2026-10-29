#!/usr/bin/env python3
"""Direct-Connection drive agent.

This small program lives on the USB flash drive next to its config file. The
recipient starts it themselves (there is no autorun); it shows a window with a
live log plus Pause and Quit, checks the server for new files, downloads them
into the drive's own ``Inbox/`` folder and verifies each SHA-256 hash.

It never writes outside Inbox/, never touches the host computer's settings, and
never runs anything it downloads. Executable file types are refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

CONFIG_NAME = "direct-connection.json"
CHUNK = 256 * 1024
BLOCKED_EXTENSIONS = frozenset(
    {
        "app", "apk", "bat", "bash", "bin", "chm", "cmd", "com", "cpl", "deb",
        "dll", "dmg", "exe", "gadget", "hta", "img", "iso", "jar", "js", "jse",
        "lnk", "msi", "pif", "pkg", "ps1", "psm1", "rpm", "scr", "sh", "url",
        "vbe", "vbs", "wsf", "wsh", "zsh",
    }
)
MAX_POLL_SECONDS = 3600
MIN_POLL_SECONDS = 5


class AgentError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller one-file build
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_config(base_dir: Path, config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or (base_dir / CONFIG_NAME)
    if not path.is_file():
        raise AgentError(
            f"Setup file not found: {path}\n"
            "Ask the drive's owner to prepare this drive again."
        )
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentError(f"Setup file is unreadable: {exc}") from exc
    for key in ("server_url", "token"):
        if not config.get(key):
            raise AgentError(f"Setup file is missing '{key}'.")
    config["server_url"] = str(config["server_url"]).rstrip("/")
    config.setdefault("inbox", "Inbox")
    poll = int(config.get("poll_seconds", 30) or 30)
    config["poll_seconds"] = max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, poll))
    return config


def safe_name(name: str) -> str:
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip().strip(". ")
    if not base or base in {".", ".."}:
        raise AgentError("Server sent an unusable filename.")
    return base


def extension_of(name: str) -> str:
    return os.path.splitext(name)[1].lower().lstrip(".")


class Agent:
    """Polls the server and saves verified files into Inbox/."""

    def __init__(
        self,
        base_dir: Path,
        config: dict[str, Any],
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.base_dir = base_dir
        inbox_name = str(config.get("inbox") or "Inbox")
        if Path(inbox_name).is_absolute() or ".." in Path(inbox_name).parts:
            raise AgentError("Config 'inbox' must be a relative folder inside the drive.")
        self.inbox = (base_dir / inbox_name).resolve()
        self.server_url = config["server_url"]
        self.token = config["token"]
        self.poll_seconds = int(config.get("poll_seconds", 30))
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.revoked = False
        self._log_cb = log or (lambda message: None)
        self.inbox.mkdir(parents=True, exist_ok=True)

    # -- logging -------------------------------------------------------

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log_cb(f"[{stamp}] {message}")

    # -- http ----------------------------------------------------------

    def _open(self, path: str, data: bytes | None = None, headers: dict[str, str] | None = None):
        url = self.server_url + path
        request = urllib.request.Request(url, data=data, headers={
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "DirectConnectionAgent/0.1",
            **(headers or {}),
        })
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail", "")
            except Exception:
                pass
            if exc.code in (401, 403):
                self.revoked = exc.code == 403
                raise AgentError(
                    "The owner revoked this drive's access." if self.revoked
                    else "The server rejected this drive's token.",
                    status=exc.code,
                ) from exc
            raise AgentError(f"Server error {exc.code}{': ' + str(detail) if detail else ''}",
                             status=exc.code) from exc
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AgentError(f"Offline or unreachable: {reason}") from exc

    def _get_json(self, path: str) -> dict[str, Any]:
        with self._open(path) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _open_url(self, url: str):
        """Fetch a short-lived storage URL; it carries its own authorisation."""
        request = urllib.request.Request(
            url, headers={"User-Agent": "DirectConnectionAgent/0.1"}
        )
        try:
            return urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as exc:
            raise AgentError(
                f"Download link failed ({exc.code}).", status=exc.code
            ) from exc
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AgentError(f"Download link unreachable: {reason}") from exc

    def fetch_info(self) -> dict[str, Any]:
        return self._get_json("/api/agent/me")

    # -- work ----------------------------------------------------------

    def check_once(self) -> int:
        """One poll cycle. Returns the number of files delivered."""
        if self.stopped.is_set() or self.revoked:
            return 0
        try:
            queue_data = self._get_json("/api/agent/queue")
        except AgentError as exc:
            self.log(str(exc))
            raise
        files = queue_data.get("files", [])
        if not files:
            return 0
        delivered = 0
        for entry in files:
            if self.stopped.is_set() or self.paused.is_set():
                break
            try:
                if self._handle(entry):
                    delivered += 1
            except AgentError as exc:
                self.log(f"Could not get {entry.get('filename', '?')}: {exc}")
                if self.revoked:
                    break
        return delivered

    def _handle(self, entry: dict[str, Any]) -> bool:
        filename = safe_name(str(entry.get("filename", "")))
        expected = str(entry.get("sha256", "")).lower()
        if not expected:
            self.log(f"Skipped {filename}: no checksum provided.")
            return False
        if extension_of(filename) in BLOCKED_EXTENSIONS:
            self.log(f"Refused {filename}: executable file types are never saved.")
            return False

        existing = self.inbox / filename
        if existing.is_file() and self.sha256_of(existing) == expected:
            self.log(f"{filename} is already in Inbox; confirming delivery.")
            self._ack(entry["id"], expected)
            return False

        target = self._unique_target(filename)
        partial = self.inbox / f".dcpart-{entry['id']}"
        self.log(f"Downloading {filename}…")
        link = self._get_json(f"/api/agent/files/{entry['id']}/link")
        download_url = urllib.parse.urljoin(
            self.server_url.rstrip("/") + "/", str(link.get("url", ""))
        )
        digest = hashlib.sha256()
        written = 0
        try:
            with self._open_url(download_url) as resp, partial.open("wb") as out:
                while True:
                    if self.stopped.is_set():
                        raise AgentError("Cancelled.")
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    written += len(chunk)
                    out.write(chunk)
            actual = digest.hexdigest()
            if actual != expected:
                raise AgentError("checksum mismatch — file discarded")
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        # Verify what actually landed on the drive before telling the owner.
        if self.sha256_of(target) != expected:
            target.unlink(missing_ok=True)
            raise AgentError("verification after writing failed — file discarded")

        self.log(f"Saved {target.name} to Inbox ({written} bytes, SHA-256 verified).")
        self._ack(entry["id"], expected)
        return True

    def _ack(self, file_id: str, sha256: str) -> None:
        body = urllib.parse.urlencode({"sha256": sha256}).encode("ascii")
        with self._open(
            f"/api/agent/files/{file_id}/ack",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ):
            pass

    def _unique_target(self, filename: str) -> Path:
        candidate = (self.inbox / filename).resolve()
        if self.inbox not in candidate.parents:
            raise AgentError("Refusing to write outside Inbox.")
        if not candidate.exists():
            return candidate
        stem, suffix = os.path.splitext(filename)
        for index in range(1, 1000):
            candidate = (self.inbox / f"{stem} ({index}){suffix}").resolve()
            if not candidate.exists():
                return candidate
        raise AgentError(f"Too many copies of {filename} already in Inbox.")

    @staticmethod
    def sha256_of(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def run_loop(self) -> None:
        self.log("Checking for new files. Files only ever land in the Inbox folder.")
        while not self.stopped.is_set() and not self.revoked:
            if self.paused.is_set():
                self.stopped.wait(1.0)
                continue
            try:
                count = self.check_once()
                if count:
                    self.log(f"{count} file(s) delivered.")
            except AgentError:
                # check_once already logged the reason; retry on the next poll.
                pass
            self.stopped.wait(self.poll_seconds)


# -- command line (no GUI) ----------------------------------------------


def run_headless(agent: Agent, once: bool) -> int:
    if once:
        try:
            count = agent.check_once()
        except AgentError:  # already logged by check_once
            return 1
        agent.log(f"Done. {count} file(s) delivered.")
        return 0
    agent.run_loop()
    return 0


# -- GUI ----------------------------------------------------------------


def run_gui(agent: Agent) -> int:
    import tkinter as tk
    from tkinter import font, ttk

    messages: "queue.Queue[str]" = queue.Queue()

    root = tk.Tk()
    # Only route logs into the window once it really exists; if Tk fails we fall
    # back to console logging in main().
    agent._log_cb = messages.put
    root.title("Direct-Connection — drive inbox")
    root.geometry("640x420")
    root.minsize(520, 320)

    heading_font = font.Font(family="TkDefaultFont", size=13, weight="bold")
    ttk.Label(root, text="This drive can receive files from its owner", font=heading_font).pack(
        anchor="w", padx=12, pady=(12, 2)
    )
    ttk.Label(
        root,
        text=(
            "Nothing runs automatically and nothing is installed. New files are saved to the "
            "Inbox folder on this drive only, and each one is checked for integrity first."
        ),
        wraplength=600,
        justify="left",
    ).pack(anchor="w", padx=12)

    status_var = tk.StringVar(value="Starting…")
    ttk.Label(root, textvariable=status_var).pack(anchor="w", padx=12, pady=(8, 4))

    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
    log_view = tk.Text(frame, height=12, wrap="word", state="disabled", relief="solid", borderwidth=1)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=log_view.yview)
    log_view.configure(yscrollcommand=scroll.set)
    log_view.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    buttons = ttk.Frame(root)
    buttons.pack(fill="x", padx=12, pady=(0, 12))

    def append(message: str) -> None:
        log_view.configure(state="normal")
        log_view.insert("end", message + "\n")
        log_view.see("end")
        log_view.configure(state="disabled")

    def toggle_pause() -> None:
        if agent.paused.is_set():
            agent.paused.clear()
            pause_button.configure(text="Pause")
            status_var.set(f"Running — checks every {agent.poll_seconds}s")
            agent.log("Resumed.")
        else:
            agent.paused.set()
            pause_button.configure(text="Resume")
            status_var.set("Paused — nothing is being downloaded")
            agent.log("Paused.")

    def check_now() -> None:
        if agent.paused.is_set():
            agent.log("Resume first to check for files.")
            return
        status_var.set("Checking for new files…")
        threading.Thread(target=_manual_check, daemon=True).start()

    def _manual_check() -> None:
        try:
            count = agent.check_once()
            messages.put(f"[check] {count} new file(s).")
        except AgentError:
            messages.put("[check] Could not check right now; see the log above.")

    def quit_app() -> None:
        agent.stopped.set()
        agent.log("Stopped.")
        root.destroy()

    pause_button = ttk.Button(buttons, text="Pause", command=toggle_pause)
    pause_button.pack(side="left")
    ttk.Button(buttons, text="Check now", command=check_now).pack(side="left", padx=6)
    ttk.Button(buttons, text="Open Inbox", command=lambda: open_folder(agent.inbox)).pack(
        side="left"
    )
    ttk.Button(buttons, text="Quit", command=quit_app).pack(side="right")

    def pump() -> None:
        try:
            while True:
                append(messages.get_nowait())
        except queue.Empty:
            pass
        if agent.revoked:
            status_var.set("Access revoked by the owner — no more downloads")
        elif agent.paused.is_set():
            status_var.set("Paused — nothing is being downloaded")
        else:
            status_var.set(f"Running — checks every {agent.poll_seconds}s")
        root.after(250, pump)

    def worker() -> None:
        try:
            info = agent.fetch_info()
            agent.log(f"Connected to server as drive {info.get('name', info.get('id'))}.")
        except AgentError as exc:
            agent.log(str(exc))
        agent.run_loop()
        if agent.revoked:
            messages.put("The owner revoked this drive. You can close this window.")

    root.protocol("WM_DELETE_WINDOW", quit_app)
    threading.Thread(target=worker, daemon=True).start()
    pump()
    root.mainloop()
    agent.stopped.set()
    return 0


def open_folder(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Direct-Connection drive agent.")
    parser.add_argument("--config", type=Path, default=None, help="path to setup file")
    parser.add_argument("--once", action="store_true", help="check once, print log, exit")
    parser.add_argument("--headless", action="store_true", help="no window (logs to stdout)")
    args = parser.parse_args(argv)

    base_dir = resolve_base_dir()
    try:
        config = load_config(base_dir, args.config)
        agent = Agent(base_dir, config, log=lambda m: print(m, flush=True))
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if not args.once and not args.headless:
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("Direct-Connection", str(exc))
                root.destroy()
            except Exception:
                pass
        return 2

    if args.once or args.headless:
        return run_headless(agent, args.once)
    try:
        return run_gui(agent)
    except Exception as exc:  # no tkinter or no display
        print(f"Window unavailable ({exc}); running headless.", file=sys.stderr)
        return run_headless(agent, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
