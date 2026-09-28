# Runs at interpreter start (Lib/site-packages/reforger.pth). The exe is
# pythonw (no console): the server runs in a thread and the UI opens as its
# own app window through Edge/Chrome app mode. Closing the window exits.
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(sys.executable))
LOG = os.path.join(HERE, "app.log")
PORT = 8765


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def desktop_shortcut():
    exe = os.path.join(HERE, "Reforger Mod Browser.exe")
    ico = os.path.join(HERE, "icon.ico")
    ps = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\Reforger Mod Browser.lnk\");"
        f"$s.TargetPath='{exe}';$s.WorkingDirectory='{HERE}';$s.IconLocation='{ico},0';"
        "$s.Description='Reforger Mod Browser';$s.Save()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, timeout=20, creationflags=0x08000000)
    except Exception as e:
        log(f"shortcut: {e}")


def port_open():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def find_browser():
    pf = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramFiles", r"C:\Program Files"),
          os.path.join(os.environ.get("LOCALAPPDATA", ""), "")]
    cands = []
    for base in pf:
        cands += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                  os.path.join(base, r"Google\Chrome\Application\chrome.exe"),
                  os.path.join(base, r"BraveSoftware\Brave-Browser\Application\brave.exe")]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def place_window(prof, x, y, w, h, maxed):
    """Write the saved size into the browser profile's own record of this app window. Edge restores
    from that record, not from --window-size, and a record that no longer fits the screen (or one a
    force-closed window never updated) makes it fall back to a small default window."""
    import json
    pth = os.path.join(prof, "Default", "Preferences")
    if not os.path.exists(pth):
        return
    with open(pth, "r", encoding="utf-8") as f:
        p = json.load(f)
    br = p.setdefault("browser", {})
    wp = br.get("window_placement") or {}
    wl, wt = int(wp.get("work_area_left", 0)), int(wp.get("work_area_top", 0))
    wr, wb = int(wp.get("work_area_right", 0)), int(wp.get("work_area_bottom", 0))
    left, top = max(x, wl), max(y, wt)
    right, bottom = left + w, top + h
    if wr > wl and wb > wt:
        right, bottom = min(right, wr), min(bottom, wb)
    rec = {"left": left, "top": top, "right": right, "bottom": bottom, "maximized": bool(maxed)}
    for k in ("work_area_left", "work_area_top", "work_area_right", "work_area_bottom"):
        if k in wp:
            rec[k] = wp[k]
    # Chromium splits the app name "127.0.0.1_/" on dots into nested keys
    node = br.setdefault("app_window_placement", {})
    for part in ("127", "0", "0"):
        node = node.setdefault(part, {})
    old = node.get("1_/") or {}
    old.update(rec)
    node["1_/"] = old
    tmp = pth + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f)
    os.replace(tmp, pth)


def open_window(url):
    b = find_browser()
    if not b:
        log("no chromium browser found; using the default browser")
        import webbrowser
        webbrowser.open(url)
        return None
    prof = os.path.join(HERE, "window-profile")
    size, pos, maxed = "1500,950", None, False
    try:
        import json
        with open(os.path.join(HERE, "window.json")) as f:
            w = json.load(f)
        maxed = bool(w.get("max"))
        wx, wy, ww, wh = int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"])
        if ww >= 600 and wh >= 400:
            place_window(prof, wx, wy, ww, wh, maxed)
            size = "%d,%d" % (ww, wh)
            if wx > -2000 and wy > -2000:
                pos = "%d,%d" % (max(wx, 0), max(wy, 0))
    except Exception as e:
        log(f"window size: {e}")
    args = [b, f"--app={url}", f"--user-data-dir={prof}", f"--window-size={size}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate,MediaRouter", "--disable-sync",
            "--autoplay-policy=no-user-gesture-required"]   # the start-up sound plays before any click
    if pos:
        args.append(f"--window-position={pos}")
    if maxed:
        args.append("--start-maximized")
    log(f"window: {b}")
    return subprocess.Popen(args, creationflags=0x08000000)


def stale_server():
    """Is the copy already running older than the files on disk? (Opening the app while it is
    still running used to just open another window on the old code.)"""
    try:
        import json
        import update
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/app/version", timeout=4) as r:
            running = json.loads(r.read()).get("version") or ""
        return running != update.disk_version()
    except urllib.error.HTTPError:
        return True        # a copy from before versions existed
    except Exception:
        return False       # unreachable: leave it alone


def replace_old_copy():
    log("an older copy is still running: closing it so the new files load")
    prof = os.path.join(HERE, "window-profile").replace("'", "''")
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains('%s') } "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % prof)
    for cmd in (["taskkill", "/F", "/IM", "Reforger Mod Browser.exe", "/FI", "PID ne %d" % os.getpid()],
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, creationflags=0x08000000)
        except Exception as e:
            log(f"close old copy: {e}")
    for _ in range(40):
        if not port_open():
            break
        time.sleep(0.25)
    time.sleep(1)


def main():
    sys.argv = [sys.argv[0]]
    url = f"http://127.0.0.1:{PORT}/"
    desktop_shortcut()
    if port_open() and stale_server():
        replace_old_copy()
    if not port_open():
        import server
        server.CONFIG["port"] = PORT
        # serve in a thread; server.main() would try to open the default browser
        server.init_db()
        server.db_health()
        server.slim_db()        # one time: shrinks the database (about a minute on the first start)
        with server.db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
        if n == 0 or not server.meta_get("full_sync_at"):
            server.SYNC.full()      # first run, or a first run that was interrupted
        elif int(server.CONFIG.get("auto_sync_minutes") or 0) > 0:
            server.SYNC.incremental()
        threading.Thread(target=server.auto_sync_loop, daemon=True).start()
        server.ENRICH.start()
        server.import_installed()
        threading.Timer(4, server.download_missing_subscriptions).start()
        threading.Timer(60, server.auto_update).start()   # after the start-up check has seen what changed
        threading.Thread(target=server._backend_check_loop, daemon=True).start()   # Bohemia's own version check
        server.party.share_start()
        threading.Thread(target=server.update.check_loop, daemon=True).start()
        server.write_queue(2)   # playlist file for the game, from the start
        if server.CONFIG.get("launch_game_on_open"):
            threading.Timer(3, server.launch_game).start()
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"server up, {n} mods")
        for _ in range(50):
            if port_open():
                break
            time.sleep(0.1)
    proc = open_window(url)
    if proc is None:
        # default browser: keep the server alive until the machine sleeps or the user kills it
        while True:
            time.sleep(3600)
    # app-mode browsers hand off to an existing instance of the same profile
    # and exit at once; poll the page's liveness instead of the process
    time.sleep(3)
    misses = 0
    while True:
        rc = proc.poll()
        if rc is None or window_alive():
            misses = 0
        else:
            misses += 1
            # one slow or odd process scan must not kill the server under an open window:
            # only quit after ~20 s of the window being gone
            if misses >= 8:
                break
        time.sleep(2.5)
    log("window closed, exiting")
    # the hidden download helper only lives while the browser is open
    try:
        import wsdl
        wsdl.stop()
    except Exception:
        pass


def window_alive():
    """Is our app window still open? The page polls the server every 3 s, so a recent poll means
    yes, for free. Only when it has gone quiet (a minimized window polls about once a minute) are
    the browser processes scanned; a scan that fails or times out counts as 'still open'."""
    try:
        import server
        if time.time() - server.UI_SEEN[0] < 15:
            return True
    except Exception:
        pass
    try:
        for exe in ("msedge.exe", "chrome.exe", "brave.exe"):
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe, "/FO", "CSV", "/V"], capture_output=True,
                                 text=True, timeout=20, creationflags=0x08000000).stdout
            if "Reforger" in out:
                return True
        return False
    except Exception:
        return True


try:
    main()
except SystemExit:
    pass
except Exception:
    import traceback
    log(traceback.format_exc())
os._exit(0)
