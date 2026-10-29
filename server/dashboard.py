"""HTML for the owner dashboard: a login page and the drive overview.

Plain HTML and a little vanilla JavaScript, so the server keeps its small
dependency list. The page fetches ``/api/dashboard/drives`` and re-renders the
table every few seconds; the action buttons call the session-protected
``/api/dashboard/drives/{id}/...`` endpoints.
"""

from __future__ import annotations

from html import escape
from string import Template

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --border: #e2e5ea;
  --text: #16181d;
  --muted: #667085;
  --accent: #2f6fed;
  --good: #12805c;
  --bad: #b42318;
  --warn: #b54708;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111318;
    --panel: #191c23;
    --border: #2a2f3a;
    --text: #e8eaee;
    --muted: #98a2b3;
    --accent: #7aa2ff;
    --good: #4ade80;
    --bad: #ff7b72;
    --warn: #f5b544;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
a { color: var(--accent); }
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 24px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
header h1 { font-size: 17px; margin: 0; letter-spacing: -0.01em; }
header .who { color: var(--muted); font-size: 13px; margin-left: 10px; }
main { max-width: 1120px; margin: 0 auto; padding: 28px 24px 48px; }
h2 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.02em; }
.sub { color: var(--muted); margin: 0 0 22px; font-size: 14px; }
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 26px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.card .label {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.card .value { font-size: 26px; font-weight: 650; margin-top: 2px; }
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: auto;
}
table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px 16px; text-align: left; vertical-align: middle; }
th {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
tbody tr + tr td { border-top: 1px solid var(--border); }
td.num { font-variant-numeric: tabular-nums; }
.drive-name { font-weight: 600; }
.drive-name a { color: inherit; text-decoration: none; }
.drive-name a:hover { text-decoration: underline; }
.crumb { margin: 0 0 14px; font-size: 14px; }
.crumb a { text-decoration: none; }
.crumb a:hover { text-decoration: underline; }
.file-name { font-weight: 600; overflow-wrap: anywhere; }
.note { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.drive-id {
  color: var(--muted);
  font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 550;
  white-space: nowrap;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.badge.online { color: var(--good); }
.badge.online .dot { background: var(--good); }
.badge.offline { color: var(--bad); }
.badge.offline .dot { background: var(--bad); }
.badge.revoked { color: var(--bad); }
.badge.revoked .dot { background: var(--bad); }
.badge.installed { color: var(--good); }
.badge.pending-install { color: var(--warn); }
.muted { color: var(--muted); }
.empty { padding: 34px 16px; text-align: center; color: var(--muted); }
footer { margin-top: 18px; font-size: 12px; color: var(--muted); }
button {
  font: inherit;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  padding: 8px 14px;
  cursor: pointer;
}
button:disabled { opacity: 0.55; cursor: default; }
button.primary {
  background: var(--accent);
  border-color: transparent;
  color: #ffffff;
  font-weight: 600;
}
button.small { padding: 5px 10px; font-size: 13px; }
button.danger { color: var(--bad); }
button:hover:not(:disabled) { filter: brightness(0.96); }
td.actions { white-space: nowrap; }
td.actions button + button { margin-left: 6px; }
.login {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}
.login form {
  width: 100%;
  max-width: 360px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 26px;
  box-shadow: 0 12px 32px rgb(16 24 40 / 0.08);
}
.login h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.02em; }
.login p { color: var(--muted); font-size: 13px; margin: 0 0 20px; }
.login label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.login input {
  width: 100%;
  font: inherit;
  padding: 10px 12px;
  margin-bottom: 16px;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.login button.primary { width: 100%; }
.error {
  background: color-mix(in srgb, var(--bad) 12%, transparent);
  color: var(--bad);
  border-radius: 8px;
  padding: 9px 12px;
  font-size: 13px;
  margin-bottom: 16px;
}
.notice {
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
  border-radius: 8px;
  padding: 9px 12px;
  font-size: 13px;
}
dialog {
  width: min(540px, calc(100vw - 32px));
  padding: 0;
  color: var(--text);
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 18px 48px rgb(16 24 40 / 0.24);
}
dialog::backdrop { background: rgb(0 0 0 / 0.45); }
#modal-body { padding: 22px; }
#modal-body h3 { margin: 0 0 6px; font-size: 18px; letter-spacing: -0.01em; }
#modal-body .hint { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
#modal-body label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
#modal-body input[type="text"] {
  width: 100%;
  font: inherit;
  padding: 9px 11px;
  margin-bottom: 14px;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
}
#modal-body input[type="file"] {
  width: 100%;
  margin-bottom: 14px;
  color: var(--text);
  font-size: 13px;
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.results { margin: 4px 0 12px; max-height: 190px; overflow: auto; }
.result { font-size: 13px; color: var(--muted); padding: 3px 0; overflow-wrap: anywhere; }
.result.ok { color: var(--good); }
.result.bad { color: var(--bad); }
code.token {
  display: block;
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 14px;
  overflow-wrap: anywhere;
}
"""

LOGIN_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in - Direct-Connection</title>
<style>$style</style>
</head>
<body>
<div class="login">
  <form method="post" action="/dashboard/login">
    <h1>Direct-Connection</h1>
    <p>Owner overview of every registered flash drive.</p>
    $message
    $body
  </form>
</div>
</body>
</html>
"""
)

LOGIN_FORM = """<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password"
       required autofocus>
<button class="primary" type="submit">Sign in</button>"""

LOGIN_DISABLED = """<div class="notice">The dashboard is disabled. Set
<code>DC_DASHBOARD_PASSWORD</code> in the server environment and restart it.</div>"""

DASHBOARD_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Drives - Direct-Connection</title>
<style>$style</style>
</head>
<body>
<header>
  <h1>Direct-Connection<span class="who">owner overview</span></h1>
  <form method="post" action="/dashboard/logout">
    <button type="submit">Sign out</button>
  </form>
</header>
<main>
  <h2>Drives</h2>
  <p class="sub">Every flash drive registered to this server. Refreshes every 15&nbsp;seconds.</p>

  <div class="cards">
    <div class="card"><div class="label">Drives</div><div class="value" id="count-total">&ndash;</div></div>
    <div class="card"><div class="label">Online</div><div class="value" id="count-online">&ndash;</div></div>
    <div class="card"><div class="label">Program installed</div><div class="value" id="count-installed">&ndash;</div></div>
    <div class="card"><div class="label">Files pending</div><div class="value" id="count-pending">&ndash;</div></div>
  </div>

  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>Drive</th>
          <th>Status</th>
          <th>Program</th>
          <th>Pending</th>
          <th>Last seen</th>
          <th>Added</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="drives">
        <tr><td colspan="7" class="empty">Loading&hellip;</td></tr>
      </tbody>
    </table>
  </div>
  <footer id="updated"></footer>
</main>

<dialog id="modal">
  <div id="modal-body"></div>
</dialog>

<script>
(function () {
  var body = document.getElementById("drives");
  var modal = document.getElementById("modal");
  var modalBody = document.getElementById("modal-body");
  var REFRESH_MS = 15000;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function button(label, className, onClick) {
    var node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  function human(size) {
    var value = Number(size) || 0;
    var units = ["B", "KiB", "MiB", "GiB"];
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return (unit === 0 ? value.toFixed(0) : value.toFixed(1)) + " " + units[unit];
  }

  function relative(iso) {
    if (!iso) return "never";
    var then = new Date(iso).getTime();
    if (isNaN(then)) return iso;
    var seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 45) return "just now";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + (minutes === 1 ? " min ago" : " mins ago");
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hours / 24);
    if (days < 30) return days + (days === 1 ? " day ago" : " days ago");
    return new Date(iso).toLocaleDateString();
  }

  function badge(kind, text, dot) {
    var span = el("span", "badge " + kind);
    if (dot !== false) span.appendChild(el("span", "dot"));
    span.appendChild(document.createTextNode(text));
    return span;
  }

  // -- modal -----------------------------------------------------------

  function openModal(nodes) {
    modalBody.textContent = "";
    nodes.forEach(function (node) { modalBody.appendChild(node); });
    if (!modal.open) modal.showModal();
  }

  function closeModal() {
    if (modal.open) modal.close();
  }

  modal.addEventListener("click", function (event) {
    if (event.target === modal) closeModal();
  });

  function actionsRow(children) {
    var row = el("div", "modal-actions");
    children.forEach(function (child) { row.appendChild(child); });
    return row;
  }

  // -- server calls ----------------------------------------------------

  var HEADERS = { "X-Dashboard-Request": "1", "Accept": "application/json" };

  function handle(response) {
    if (response.status === 401) {
      window.location = "/dashboard";
      throw new Error("Signed out.");
    }
    return response.json().catch(function () { return {}; }).then(function (payload) {
      if (!response.ok) throw new Error(payload.detail || ("HTTP " + response.status));
      return payload;
    });
  }

  function act(drive, action) {
    return fetch("/api/dashboard/drives/" + encodeURIComponent(drive.id) + "/" + action, {
      method: "POST",
      headers: HEADERS
    }).then(handle);
  }

  // -- actions ---------------------------------------------------------

  function openSend(drive) {
    var title = el("h3", null, "Send files to " + drive.name);
    var hint = el("p", "hint",
      "Files wait here until the drive is next opened. Executables and scripts are refused.");

    var fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.multiple = true;

    var noteLabel = el("label", null, "Note (optional)");
    noteLabel.htmlFor = "send-note";
    var note = document.createElement("input");
    note.type = "text";
    note.id = "send-note";
    note.maxLength = 2000;
    note.placeholder = "A short message for the agent log";

    var results = el("div", "results");
    var send = button("Send", "primary small", submit);
    var cancel = button("Cancel", "small", closeModal);

    function hex(buffer) {
      return Array.prototype.map.call(new Uint8Array(buffer), function (byte) {
        return byte.toString(16).padStart(2, "0");
      }).join("");
    }

    async function uploadOne(file, noteValue) {
      if (!window.crypto || !window.crypto.subtle) {
        throw new Error("hashing needs HTTPS in this browser");
      }
      var sha256 = hex(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()));
      var intent = await fetch(
        "/api/drives/" + encodeURIComponent(drive.id) + "/files/upload-url", {
          method: "POST",
          headers: { "X-Dashboard-Request": "1", "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name, size: file.size, note: noteValue })
        }).then(handle);

      var put = await fetch(intent.upload_url, {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream" },
        body: file
      });
      if (!put.ok) {
        throw new Error("storage rejected the upload (HTTP " + put.status + ")");
      }

      return fetch(
        "/api/drives/" + encodeURIComponent(drive.id) + "/files/" +
        encodeURIComponent(intent.id) + "/complete", {
          method: "POST",
          headers: { "X-Dashboard-Request": "1", "Content-Type": "application/json" },
          body: JSON.stringify({ sha256: sha256 })
        }).then(handle);
    }

    async function submit() {
      var files = fileInput.files ? Array.prototype.slice.call(fileInput.files) : [];
      if (!files.length) {
        results.textContent = "Choose at least one file.";
        return;
      }
      send.disabled = true;
      cancel.disabled = true;
      results.textContent = "";
      var noteValue = note.value.trim();
      var failures = 0;

      for (var index = 0; index < files.length; index += 1) {
        var file = files[index];
        var line = el("div", "result", file.name + " - uploading...");
        results.appendChild(line);
        try {
          var record = await uploadOne(file, noteValue);
          line.className = "result ok";
          line.textContent = record.filename + " - queued (" + human(record.size) + ")";
        } catch (error) {
          failures += 1;
          line.className = "result bad";
          line.textContent = file.name + " - " + error.message;
        }
      }

      send.disabled = false;
      cancel.disabled = false;
      if (!failures) send.textContent = "Done";
      load();
    }

    openModal([title, hint, fileInput, noteLabel, note, results,
               actionsRow([cancel, send])]);
  }

  function openRevoke(drive) {
    var title = el("h3", null, "Revoke " + drive.name + "?");
    var hint = el("p", "hint",
      "Its agent will stop downloading immediately and this cannot be undone. " +
      "Queued files are kept on the server.");
    var revoke = button("Revoke drive", "danger small", function () {
      revoke.disabled = true;
      act(drive, "revoke").then(function () {
        closeModal();
        load();
      }).catch(function (error) {
        hint.className = "hint";
        hint.style.color = "var(--bad)";
        hint.textContent = error.message;
        revoke.disabled = false;
      });
    });
    var cancel = button("Cancel", "small", closeModal);
    openModal([title, hint, actionsRow([cancel, revoke])]);
  }

  function openRotate(drive) {
    var title = el("h3", null, "Rotate the token for " + drive.name + "?");
    var hint = el("p", "hint",
      "The old token stops working at once. You will see the new one once, so have " +
      "the drive at hand to update direct-connection.json.");
    var rotate = button("Rotate token", "small", function () {
      rotate.disabled = true;
      act(drive, "rotate-token").then(function (result) {
        showToken(drive, result.token);
        load();
      }).catch(function (error) {
        hint.style.color = "var(--bad)";
        hint.textContent = error.message;
        rotate.disabled = false;
      });
    });
    var cancel = button("Cancel", "small", closeModal);
    openModal([title, hint, actionsRow([cancel, rotate])]);
  }

  function showToken(drive, token) {
    var title = el("h3", null, "New token for " + drive.name);
    var hint = el("p", "hint",
      "This is shown once and cannot be recovered. Update the drive with: " +
      "dc.py provision <drive> --drive-id " + drive.id + " --token <token>");
    var code = el("code", "token", token);
    var copy = button("Copy token", "small", function () {
      navigator.clipboard.writeText(token).then(function () {
        copy.textContent = "Copied";
      }).catch(function () {
        copy.textContent = "Select and copy manually";
      });
    });
    var done = button("Done", "small", closeModal);
    openModal([title, hint, code, actionsRow([copy, done])]);
  }

  // -- table -----------------------------------------------------------

  function statusCell(drive) {
    var cell = el("td");
    if (drive.revoked) cell.appendChild(badge("revoked", "Revoked"));
    else if (drive.online) cell.appendChild(badge("online", "Online"));
    else cell.appendChild(badge("offline", "Offline"));
    return cell;
  }

  function actionsCell(drive) {
    var cell = el("td", "actions");
    if (drive.revoked) {
      cell.appendChild(el("span", "muted", "No actions"));
      return cell;
    }
    cell.appendChild(button("Send", "small", function () { openSend(drive); }));
    cell.appendChild(button("Revoke", "small danger", function () { openRevoke(drive); }));
    cell.appendChild(button("Rotate", "small", function () { openRotate(drive); }));
    return cell;
  }

  function render(drives) {
    body.textContent = "";
    if (!drives.length) {
      var emptyRow = el("tr");
      var emptyCell = el("td", "empty",
        "No drives registered yet. Create one with owner-cli/dc.py.");
      emptyCell.colSpan = 7;
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
    }
    drives.forEach(function (drive) {
      var row = el("tr");

      var nameCell = el("td");
      var nameWrap = el("div", "drive-name");
      var link = el("a", null, drive.name);
      link.href = "/dashboard/drives/" + encodeURIComponent(drive.id);
      nameWrap.appendChild(link);
      nameCell.appendChild(nameWrap);
      nameCell.appendChild(el("div", "drive-id", drive.id));
      row.appendChild(nameCell);

      row.appendChild(statusCell(drive));

      var programCell = el("td");
      if (drive.installed) {
        programCell.appendChild(badge("installed", "Installed", false));
        if (!drive.agent_seen) {
          programCell.appendChild(el("div", "muted", "not opened yet"));
        }
      } else {
        programCell.appendChild(badge("pending-install", "Not installed", false));
      }
      row.appendChild(programCell);

      row.appendChild(el("td", "num",
        String(drive.pending_count == null ? 0 : drive.pending_count)));

      var seen = el("td", "muted", relative(drive.last_seen_at));
      seen.title = drive.last_seen_at || "never";
      row.appendChild(seen);

      row.appendChild(el("td", "muted", relative(drive.created_at)));
      row.appendChild(actionsCell(drive));
      body.appendChild(row);
    });

    var online = drives.filter(function (d) { return d.online; }).length;
    var installed = drives.filter(function (d) { return d.installed; }).length;
    var pending = drives.reduce(function (sum, d) { return sum + (d.pending_count || 0); }, 0);
    document.getElementById("count-total").textContent = String(drives.length);
    document.getElementById("count-online").textContent = String(online);
    document.getElementById("count-installed").textContent = String(installed);
    document.getElementById("count-pending").textContent = String(pending);
    document.getElementById("updated").textContent =
      "Last updated " + new Date().toLocaleTimeString();
  }

  function load() {
    fetch("/api/dashboard/drives", { headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (response.status === 401) { window.location = "/dashboard"; throw new Error("signed out"); }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) { render(data.drives || []); })
      .catch(function (error) {
        if (error.message !== "signed out") {
          document.getElementById("updated").textContent = "Could not refresh: " + error.message;
        }
      });
  }

  load();
  setInterval(load, REFRESH_MS);
})();
</script>
</body>
</html>
"""
)


DRIVE_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>$name - Direct-Connection</title>
<style>$style</style>
</head>
<body>
<header>
  <h1>Direct-Connection<span class="who">drive detail</span></h1>
  <form method="post" action="/dashboard/logout">
    <button type="submit">Sign out</button>
  </form>
</header>
<main id="drive" data-drive-id="$id">
  <p class="crumb"><a href="/dashboard">&larr; All drives</a></p>
  <h2>$name</h2>
  <p class="sub"><span class="drive-id">$id</span></p>

  <div class="cards">
    <div class="card"><div class="label">Status</div><div class="value" id="status">&ndash;</div></div>
    <div class="card"><div class="label">Program</div><div class="value" id="program">&ndash;</div></div>
    <div class="card"><div class="label">Files</div><div class="value" id="file-count">&ndash;</div></div>
    <div class="card"><div class="label">Pending</div><div class="value" id="pending">&ndash;</div></div>
  </div>

  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>File</th>
          <th>Size</th>
          <th>Note</th>
          <th>Status</th>
          <th>Queued</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="file-rows">
        <tr><td colspan="6" class="empty">Loading&hellip;</td></tr>
      </tbody>
    </table>
  </div>
  <footer id="updated"></footer>
</main>
<script>
(function () {
  var main = document.getElementById("drive");
  var driveId = main.dataset.driveId;
  var rows = document.getElementById("file-rows");
  var REFRESH_MS = 15000;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function human(size) {
    var value = Number(size) || 0;
    var units = ["B", "KiB", "MiB", "GiB"];
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return (unit === 0 ? value.toFixed(0) : value.toFixed(1)) + " " + units[unit];
  }

  function relative(iso) {
    if (!iso) return "never";
    var then = new Date(iso).getTime();
    if (isNaN(then)) return iso;
    var seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 45) return "just now";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + (minutes === 1 ? " min ago" : " mins ago");
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hours / 24);
    if (days < 30) return days + (days === 1 ? " day ago" : " days ago");
    return new Date(iso).toLocaleDateString();
  }

  function badge(kind, text, dot) {
    var span = el("span", "badge " + kind);
    if (dot !== false) span.appendChild(el("span", "dot"));
    span.appendChild(document.createTextNode(text));
    return span;
  }

  function removeFile(file) {
    if (!window.confirm("Remove " + file.filename + "? The stored bytes are deleted too.")) {
      return;
    }
    fetch("/api/dashboard/drives/" + encodeURIComponent(driveId) + "/files/" +
          encodeURIComponent(file.id), {
      method: "DELETE",
      headers: { "X-Dashboard-Request": "1", "Accept": "application/json" }
    }).then(function (response) {
      if (response.status === 401) { window.location = "/dashboard"; return; }
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) throw new Error(payload.detail || ("HTTP " + response.status));
        load();
      });
    }).catch(function (error) {
      document.getElementById("updated").textContent = "Could not remove: " + error.message;
    });
  }

  function render(drive) {
    var status = document.getElementById("status");
    status.textContent = "";
    if (drive.revoked) status.appendChild(badge("revoked", "Revoked"));
    else if (drive.online) status.appendChild(badge("online", "Online"));
    else status.appendChild(badge("offline", "Offline"));

    document.getElementById("program").textContent = drive.installed ? "Installed" : "Not installed";

    var files = drive.files || [];
    var pending = files.filter(function (f) { return !f.delivered_at; }).length;
    document.getElementById("file-count").textContent = String(files.length);
    document.getElementById("pending").textContent = String(pending);

    rows.textContent = "";
    if (!files.length) {
      var emptyRow = el("tr");
      var emptyCell = el("td", "empty", "No files queued for this drive yet.");
      emptyCell.colSpan = 6;
      emptyRow.appendChild(emptyCell);
      rows.appendChild(emptyRow);
    }

    files.forEach(function (file) {
      var row = el("tr");
      row.appendChild(el("td", "file-name", file.filename));
      row.appendChild(el("td", "num", human(file.size)));
      row.appendChild(el("td", "note", file.note || ""));

      var statusCell = el("td");
      if (file.delivered_at) {
        statusCell.appendChild(badge("online", "Delivered " + relative(file.delivered_at), false));
      } else {
        statusCell.appendChild(badge("pending-install", "Pending", false));
      }
      row.appendChild(statusCell);

      row.appendChild(el("td", "muted", relative(file.created_at)));

      var actionCell = el("td", "actions");
      var remove = el("button", "small danger", "Remove");
      remove.type = "button";
      remove.addEventListener("click", function () { removeFile(file); });
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      rows.appendChild(row);
    });

    document.getElementById("updated").textContent =
      "Last updated " + new Date().toLocaleTimeString();
  }

  function load() {
    fetch("/api/dashboard/drives/" + encodeURIComponent(driveId),
          { headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (response.status === 401) { window.location = "/dashboard"; throw new Error("signed out"); }
        if (response.status === 404) { window.location = "/dashboard"; throw new Error("gone"); }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(render)
      .catch(function (error) {
        if (error.message !== "signed out" && error.message !== "gone") {
          document.getElementById("updated").textContent = "Could not refresh: " + error.message;
        }
      });
  }

  load();
  setInterval(load, REFRESH_MS);
})();
</script>
</body>
</html>
"""
)


def render_drive(drive: dict) -> str:
    return DRIVE_PAGE.substitute(
        style=STYLE,
        name=escape(str(drive["name"])),
        id=escape(str(drive["id"])),
    )


def render_login(error: str | None = None, *, enabled: bool = True) -> str:
    if not enabled:
        message, body = "", LOGIN_DISABLED
    else:
        message = f'<div class="error">{escape(error)}</div>' if error else ""
        body = LOGIN_FORM
    return LOGIN_PAGE.substitute(style=STYLE, message=message, body=body)


def render_dashboard() -> str:
    return DASHBOARD_PAGE.substitute(style=STYLE)
