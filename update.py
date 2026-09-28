"""App updates over Radmin.

The source PC (the one the app is developed on) offers its own app files on the party
port whenever its browser is open. Every other copy checks the address it last joined a
party at, compares file hashes, and offers an update. Applying it downloads the changed
files, then a small script closes the app, swaps them in and starts it again.

Only app files are shared or replaced: code, the page, the companion mod. Never the
database, settings, playlists or anything else of yours.
"""
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["ui.html", "server.py", "play.py", "party.py", "wsdl.py", "update.py", "launcher.py", "README.txt", "banner.jpg"]
COMPANION = os.path.join("companion", "BetterWorkshop")
STAGE = os.path.join(HERE, "_update")

log = print
CFG = {}
STATE = {"available": False, "source": "", "files": [], "size": 0, "checked_at": 0, "error": "", "applying": False,
         "source_name": ""}


_SNAP = {}   # rel -> bytes: the app files as they were when this copy started


def init(config, logger):
    global CFG, log
    CFG = config
    log = logger
    _take_snapshot()


def _take_snapshot():
    """Friends get the version this PC is actually running, not files edited since: an update
    is only offered after the source restarts its browser."""
    snap = {}
    for rel in _app_files():
        try:
            with open(os.path.join(HERE, *rel.split("/")), "rb") as f:
                snap[rel] = f.read()
        except OSError:
            pass
    _SNAP.clear()
    _SNAP.update(snap)


def is_source():
    """The developer's copy never updates itself from anyone."""
    return bool(CFG.get("update_source"))


def _app_files():
    out = [f for f in FILES if os.path.isfile(os.path.join(HERE, f))]
    snd = os.path.join(HERE, "sounds")
    if os.path.isdir(snd):
        out += ["sounds/" + n for n in sorted(os.listdir(snd)) if n.endswith(".mp3")]
    root = os.path.join(HERE, COMPANION)
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in files:
            out.append(os.path.relpath(os.path.join(r, n), HERE).replace("\\", "/"))
    return out


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest():
    files = {}
    for rel in _app_files():
        p = os.path.join(HERE, *rel.split("/"))
        try:
            files[rel] = {"sha1": _sha1(p), "size": os.path.getsize(p)}
        except OSError:
            pass
    return {"files": files, "at": time.time()}


# ----------------------------------------------------------------------------
# source side: answered by the party share server (party.ShareHandler)
# ----------------------------------------------------------------------------
def serve(handler, path, query, name):
    if path == "/app/manifest":
        m = {"files": {rel: {"sha1": hashlib.sha1(b).hexdigest(), "size": len(b)} for rel, b in _SNAP.items()},
             "at": time.time()}
        m["name"] = name
        b = json.dumps(m).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(b)))
        handler.end_headers()
        handler.wfile.write(b)
        return True
    if path == "/app/file":
        rel = query.get("name") or ""
        if rel not in _SNAP:
            handler.send_response(403)
            handler.end_headers()
            return True
        data = _SNAP[rel]
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True
    return False


# ----------------------------------------------------------------------------
# receiving side
# ----------------------------------------------------------------------------
def _source_addr():
    a = (CFG.get("update_from") or "").strip()
    if not a:
        try:
            import party
            a = party.prefs().get("last_host") or ""
        except Exception:
            a = ""
    if a and ":" not in a:
        a = "%s:%d" % (a, int(CFG.get("party_port") or 8766))
    return a


def check():
    if is_source():
        return STATE
    addr = _source_addr()
    if not addr:
        STATE.update(available=False, error="", source="")
        return STATE
    try:
        with urllib.request.urlopen("http://%s/app/manifest" % addr, timeout=6) as r:
            theirs = json.loads(r.read())
    except Exception as e:
        # the host's browser is simply not open right now: not an error worth showing
        STATE.update(checked_at=time.time(), error="", source=addr)
        return STATE
    mine = manifest()["files"]
    changed = [rel for rel, meta in theirs["files"].items() if (mine.get(rel) or {}).get("sha1") != meta["sha1"]]
    STATE.update(available=bool(changed), source=addr, files=changed, source_name=theirs.get("name") or "",
                 size=sum(theirs["files"][r]["size"] for r in changed), checked_at=time.time(), error="")
    return STATE


def check_loop():
    time.sleep(8)
    while True:
        try:
            if not STATE["applying"]:
                check()
        except Exception as e:
            STATE["error"] = str(e)
        time.sleep(15)


def apply():
    """Download the changed files, then hand over to a script that closes the app, copies
    them in and starts it again."""
    if os.name != "nt":
        return {"ok": False, "error": "Updates run on Windows only."}
    st = check()
    if not st["available"]:
        return {"ok": False, "error": "No update available right now (is the host's browser open?)."}
    STATE["applying"] = True
    try:
        import shutil
        if os.path.isdir(STAGE):
            shutil.rmtree(STAGE)
        for rel in st["files"]:
            dst = os.path.join(STAGE, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            q = urllib.parse.urlencode({"name": rel})
            with urllib.request.urlopen("http://%s/app/file?%s" % (st["source"], q), timeout=30) as r, open(dst, "wb") as f:
                f.write(r.read())
        _run_restart_script(copy_stage=True)
        log("[update] applying", len(st["files"]), "file(s) from", st["source"])
        return {"ok": True, "files": st["files"]}
    except Exception as e:
        STATE["applying"] = False
        return {"ok": False, "error": "Update failed: %s" % e}


def _run_restart_script(copy_stage):
    """A small script that closes this app (server and window), optionally copies the staged
    update in, and starts it again."""
    exe = os.path.join(HERE, "Reforger Mod Browser.exe")
    bat = os.path.join(HERE, "_apply_update.cmd")
    # close the app window: the browser processes using THIS copy's window profile, whatever
    # the install folder is called (friends' copies are not in a "ReforgerModBrowser" folder)
    prof = os.path.join(HERE, "window-profile").replace("'", "''")
    ps_close = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains('%s') } "
                "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % prof)
    lines = [
        "@echo off",
        "title Restarting Reforger Mod Browser",
        'cd /d "%s"' % HERE,
        "timeout /t 2 /nobreak >nul",
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "%s"' % ps_close,
        'taskkill /F /IM "Reforger Mod Browser.exe" >nul 2>nul',
        'taskkill /F /FI "WINDOWTITLE eq Reforger Mod Browser*" /IM msedge.exe >nul 2>nul',
        'taskkill /F /FI "WINDOWTITLE eq Reforger Mod Browser*" /IM chrome.exe >nul 2>nul',
        "timeout /t 2 /nobreak >nul",
    ]
    if copy_stage:
        # no trailing backslash on the target: "...\" would be read as an escaped quote
        lines += ['xcopy /E /Y /Q /I "%s\\*" "%s" >nul' % (STAGE, HERE),
                  'rmdir /S /Q "%s"' % STAGE]
    lines += ['start "" "%s"' % exe, 'del "%~f0"']
    with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")   # newline= turns each \n into \r\n
    subprocess.Popen(["cmd", "/c", bat], cwd=HERE, creationflags=0x00000008 | 0x08000000)


def local_changes():
    """Source copy: app files edited on disk since this copy started (not running yet, and not
    offered to friends yet)."""
    changed = []
    for rel in _app_files():
        try:
            with open(os.path.join(HERE, *rel.split("/")), "rb") as f:
                data = f.read()
        except OSError:
            continue
        if _SNAP.get(rel) != data:
            changed.append(rel)
    changed += [rel for rel in _SNAP if not os.path.exists(os.path.join(HERE, *rel.split("/")))]
    return changed


def restart():
    """Close and reopen this copy so it runs (and offers the party) the current files."""
    if os.name != "nt":
        return {"ok": False, "error": "Restart works on Windows only."}
    STATE["applying"] = True
    try:
        _run_restart_script(copy_stage=False)
        return {"ok": True}
    except Exception as e:
        STATE["applying"] = False
        return {"ok": False, "error": "Restart failed: %s" % e}


def status():
    s = dict(STATE)
    s["is_source"] = is_source()
    if s["is_source"]:
        s["local_changes"] = local_changes()
    return s


def _version_of(snap):
    h = hashlib.sha1()
    for rel in sorted(snap):
        h.update(rel.encode() + b"\0" + snap[rel] + b"\0")
    return h.hexdigest()[:16]


def running_version():
    """The app files this running copy started with."""
    return _version_of(_SNAP)


def disk_version():
    """The app files on disk now."""
    snap = {}
    for rel in _app_files():
        try:
            with open(os.path.join(HERE, *rel.split("/")), "rb") as f:
                snap[rel] = f.read()
        except OSError:
            pass
    return _version_of(snap)
