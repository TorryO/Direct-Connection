# Direct-Connection
This allows the owner of the flash to remotely upload content into the flash once connected to a device with internet access.
Send files to a USB flash drive **after you have handed it to someone else**. When the drive is plugged into any computer with internet, the owner can push new content to it remotely.

Think of it as "Dropbox for a flash drive": the owner uploads files online, and the drive pulls them down the next time the person it was given to opens it.

## How it works

```
Owner (web dashboard / CLI)
        |  uploads files for Drive #ID
        v
Server (API + file storage)
        ^  drive checks for new files over HTTPS
        |
Drive Agent (portable app stored ON the flash drive)
        |  downloads files into the drive's Inbox/ folder
        v
Flash drive  ->  recipient opens Inbox/ and sees the new content
```

1. **Owner dashboard**: log in, pick a drive, upload files or write a note.
2. **Server**: stores uploads and a per-drive queue of "pending files".
3. **Drive Agent**: a small portable program kept on the flash drive. The recipient starts it, it checks the server, and it downloads anything new into the drive.

## Trust and safety rules (non-negotiable)

This tool writes to someone else's computer session, so it must be transparent and harmless by design:

- **No autorun.** The agent only runs when the recipient opens it themselves.
- **Visible at all times.** It shows a window or tray icon with a live activity log, plus Pause and Quit buttons.
- **Writes only inside the drive's `Inbox/` folder.** It never touches the host computer's files, settings, or startup items.
- **Never executes what it downloads.** No `.exe`, scripts, or installers are run. Executable file types can be blocked entirely.
- **Integrity checks.** Every file is verified with a SHA-256 hash before being saved.
- **Revocable.** Each drive has its own token that the owner can disable at any time.
- **Recipient-friendly.** The first-run screen explains what the drive does and who the owner is.

## MVP scope (version 0.1)

- [ ] Server API: register drive, upload file for a drive, list pending files, download file
- [ ] Owner CLI or simple web page to upload files to a drive ID
- [ ] Drive Agent: check for new files, download to `Inbox/`, verify hash, show log
- [ ] Per-drive token authentication over HTTPS
- [ ] Basic file size and type limits

## Roadmap

- Owner web dashboard with login and drive management
- Offline queue: files wait on the server until the drive connects
- Delivery receipts (owner sees when a file arrived)
- Optional end-to-end encryption of files
- Auto-update for the agent
- Windows, macOS, and Linux builds of the agent

## Suggested stack

- **Server:** Python (FastAPI) or Node (Express), SQLite to start, local disk or S3-compatible storage for files
- **Drive Agent:** Python or Node, packaged as a single portable executable (PyInstaller or `pkg`)
- **Owner interface:** CLI first, web dashboard later

## Project structure

```
Direct-Connection/
  server/        # API and storage
  agent/         # portable app that lives on the flash drive
  owner-cli/     # upload tool for the owner
  docs/          # design notes
  README.md
```

## Getting started

```bash
git clone https://github.com/TorryO/Direct-Connection.git
cd Direct-Connection
opencode
```

## License

MIT (add a LICENSE file)
