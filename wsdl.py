"""Workshop downloads without the game.

Bohemia publishes no download API; the two programs that download Workshop
mods are the game and the free Arma Reforger Server (Steam tool, app 1874900).
This runs the server hidden (no window), with a throwaway config that lists
the mods to fetch, pointed at the game's own addons folder. When every mod is
on disk the server is stopped. It only ever runs while the browser is open.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import play

log = print
CFG = {}
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_download")
SERVER_APP_ID = 1874900
# a stock scenario the server can load; it only matters that the config is valid
DUMMY_SCENARIO = "{ECC61978EDCC2B5A}Missions/23_Campaign.conf"

STATE = {"running": False, "mods": [], "done": 0, "total": 0, "bytes": 0, "expected": 0, "state": "",
         "error": "", "failed": [], "started_at": 0, "finished_at": 0, "log_tail": [], "speed": 0}
HISTORY = []   # finished downloads this session, newest first
PENDING = []   # asked for while a download was running; started as soon as it ends
_proc = [None]
_lock = threading.Lock()


def init(config, logger):
    global CFG, log
    CFG = config
    log = logger


def server_exe():
    s = CFG.get("server_exe")
    if s and os.path.exists(s):
        return s
    for lib in play.steam_libraries():
        p = os.path.join(lib, "steamapps", "common", "Arma Reforger Server", "ArmaReforgerServer.exe")
        if os.path.exists(p):
            return p
    return ""


def install():
    """Ask Steam to install the free Arma Reforger Server tool."""
    try:
        os.startfile("steam://install/%d" % SERVER_APP_ID)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": "Could not open Steam: %s" % e}


def _mod_folder(guid):
    d = play.addons_dir()
    for n in play._listdir(d):
        if n.upper().endswith("_" + guid):
            return os.path.join(d, n)
    return ""


def _installed_version(folder):
    for name in ("ServerData.json", "meta"):
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8-sig") as f:
                j = json.load(f)
        except Exception:
            continue
        if name == "ServerData.json":
            v = (j.get("revision") or {}).get("version")
        else:
            v = ((j.get("meta") or {}).get("versions") or [{}])[0].get("version")
        if v:
            return v
    return ""


def _complete(m):
    f = _mod_folder(m["guid"])
    if not f or not os.path.exists(os.path.join(f, "data.pak")) or not os.path.exists(os.path.join(f, "addon.gproj")):
        return False
    if m.get("version") and _installed_version(f) != m["version"]:
        return False
    if not play.files_complete(f):
        return False
    # the server assembles files in its temp folder first; anything left there means not done
    if _dir_bytes(os.path.join(WORK, "temp", m["guid"])) > 0:
        return False
    return True


def _dir_bytes(d):
    n = 0
    for root, _, files in os.walk(d):
        for x in files:
            try:
                n += os.path.getsize(os.path.join(root, x))
            except OSError:
                pass
    return n


def _folder_bytes(guid):
    """Bytes on disk for this mod so far: the finished folder plus the server's temp work."""
    f = _mod_folder(guid)
    return (_dir_bytes(f) if f else 0) + _dir_bytes(os.path.join(WORK, "temp", guid))


def start(mods, check=False):
    """mods: [{guid, name, version (optional, exact), size (optional, for progress)}]
    check=True: an update check straight against Bohemia's backend (the same one the game uses,
    which learns about a new version long before the Workshop website does). Every listed mod goes
    to the server with no version, so it fetches whatever is newer than what is on disk."""
    with _lock:
        if STATE["running"] and check:
            return {"ok": False, "busy": True, "error": "A download is running; try again when it finishes."}
        if STATE["running"]:
            have = {m["guid"] for m in STATE["mods"]} | {m["guid"] for m in PENDING}
            added = [m for m in mods if m["guid"] not in have]
            PENDING.extend(added)
            return {"ok": True, "queued": len(added)}
        exe = server_exe()
        if not exe:
            return {"ok": False, "need_install": True,
                    "error": "The free Arma Reforger Server tool is needed to download without the game. Click Install (Steam opens), then try again."}
        if check:
            todo = []
            for m in mods:
                x = dict(m, check=True)
                x.pop("version", None)
                f = _mod_folder(m["guid"])
                x["before"] = _installed_version(f) if f else ""
                todo.append(x)
        else:
            todo = [m for m in mods if not _complete(m)]
        if not todo:
            return {"ok": True, "nothing": True}
        if os.path.isdir(WORK):
            shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(os.path.join(WORK, "profile"), exist_ok=True)
        port = int(CFG.get("download_port") or 2311)
        cfg = {
            "bindAddress": "127.0.0.1", "bindPort": port, "publicAddress": "127.0.0.1", "publicPort": port,
            "a2s": {"address": "127.0.0.1", "port": port + 1},
            "game": {"name": "Reforger Mod Browser download", "password": "", "passwordAdmin": "", "admins": [],
                     "scenarioId": DUMMY_SCENARIO, "maxPlayers": 1, "visible": False, "gameProperties": {},
                     "mods": [dict({"modId": m["guid"], "name": m.get("name") or m["guid"]},
                                   **({"version": m["version"]} if m.get("version") else {})) for m in todo]},
            "operating": {"disableCrashReporter": True},
        }
        cpath = os.path.join(WORK, "download_config.json")
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        target_root = os.path.dirname(play.addons_dir().rstrip("\\/"))   # the server adds \addons
        args = [exe, "-config", cpath, "-profile", os.path.join(WORK, "profile"), "-addonDownloadDir", target_root,
                "-addonTempDir", os.path.join(WORK, "temp"), "-maxFPS", "10"]
        log("[download] starting hidden server:", subprocess.list2cmdline(args))
        try:
            _proc[0] = subprocess.Popen(args, cwd=os.path.dirname(exe), creationflags=0x08000000 if os.name == "nt" else 0,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return {"ok": False, "error": "Could not start the download helper: %s" % e}
        STATE.update(running=True, mods=[dict(m, done=False, bytes=0, started=0) for m in todo], done=0, total=len(todo), bytes=0, speed=0,
                     expected=sum(int(m.get("size") or 0) for m in todo), state="starting", error="", failed=[],
                     started_at=time.time(), finished_at=0, log_tail=[], mode="check" if check else "download",
                     updated=[])
        threading.Thread(target=_watch, daemon=True).start()
        return {"ok": True}


def _server_log():
    d = os.path.join(WORK, "profile", "logs")
    try:
        runs = sorted((os.path.join(d, n) for n in os.listdir(d)), key=os.path.getmtime)
    except OSError:
        return ""
    if not runs:
        return ""
    try:
        with open(os.path.join(runs[-1], "console.log"), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


_BAD = re.compile(r"(addons are not downloadable|Cannot start until they are removed|Failed to download|"
                  r"ResourceNotFoundError|Invalid config)", re.I)


def _watch():
    last_bytes, last_change = -1, time.time()
    try:
        while True:
            time.sleep(2)
            p = _proc[0]
            done = 0
            check = STATE.get("mode") == "check"
            for m in STATE["mods"]:
                if not m["done"]:
                    # an update check only counts what is actually coming down
                    m["bytes"] = _dir_bytes(os.path.join(WORK, "temp", m["guid"])) if check else _folder_bytes(m["guid"])
                    if m["bytes"] and not m["started"]:
                        m["started"] = time.time()
                if not check and not m["done"] and _complete(m):
                    m["done"] = True
                    HISTORY.insert(0, {"guid": m["guid"], "name": m.get("name"), "size": m["bytes"], "at": time.time(),
                                       "thumb": m.get("thumb") or "", "source": "Workshop"})
                    log("[download] finished", m.get("name"))
                    play.invalidate_scan()
                done += 1 if m["done"] else 0
            STATE["done"] = done
            prev = STATE["bytes"]
            STATE["bytes"] = sum(m["bytes"] for m in STATE["mods"])
            STATE["speed"] = max(0, (STATE["bytes"] - prev) / 2.0)
            text = _server_log()
            lines = [l for l in text.splitlines() if l.strip()]
            STATE["log_tail"] = lines[-6:]
            bad = [l for l in lines if _BAD.search(l)]
            # The server finished downloading and is about to load every mod and compile their
            # scripts (which can pop up compile-error boxes). We only wanted the files: stop it now.
            if "Required addons are ready to use" in text and check:
                stop()
                time.sleep(2)
                for m in STATE["mods"]:
                    f = _mod_folder(m["guid"])
                    after = _installed_version(f) if f else ""
                    if after and after != m.get("before"):
                        STATE["updated"].append("%s %s -> %s" % (m.get("name") or m["guid"], m.get("before") or "?", after))
                        HISTORY.insert(0, {"guid": m["guid"], "name": m.get("name"), "size": _folder_bytes(m["guid"]), "at": time.time(),
                                           "thumb": m.get("thumb") or "", "source": "Update " + after})
                    m["done"] = True
                play.invalidate_scan()
                STATE["done"] = STATE["total"]
                STATE["state"] = "done"
                log("[download] update check:", "; ".join(STATE["updated"]) or "everything up to date")
                break
            if "Required addons are ready to use" in text:
                stop()
                for _ in range(10):
                    for m in STATE["mods"]:
                        if not m["done"] and _complete(m):
                            m["done"] = True
                            m["bytes"] = _folder_bytes(m["guid"])
                            HISTORY.insert(0, {"guid": m["guid"], "name": m.get("name"), "size": m["bytes"], "at": time.time(),
                                               "thumb": m.get("thumb") or "", "source": "Workshop"})
                            log("[download] finished", m.get("name"))
                            play.invalidate_scan()
                    if all(m["done"] for m in STATE["mods"]):
                        break
                    time.sleep(1)
                STATE["done"] = sum(1 for m in STATE["mods"] if m["done"])
                STATE["bytes"] = sum(m["bytes"] for m in STATE["mods"])
                if STATE["done"] == STATE["total"]:
                    STATE["state"] = "done"
                else:
                    STATE["failed"] = [m["guid"] for m in STATE["mods"] if not m["done"]]
                    STATE["error"] = "%d mod(s) did not finish downloading." % len(STATE["failed"])
                break
            if done == STATE["total"]:
                STATE["state"] = "done"
                break
            STATE["state"] = "downloading" if lines else "starting"
            if bad and (time.time() - STATE["started_at"] > 20):
                STATE["error"] = bad[-1].split(":", 3)[-1].strip()[:300]
                STATE["failed"] = [m["guid"] for m in STATE["mods"] if not m["done"]]
                break
            if p and p.poll() is not None:
                STATE["error"] = "The download helper stopped early." + ((" Last log line: " + lines[-1][:200]) if lines else "")
                STATE["failed"] = [m["guid"] for m in STATE["mods"] if not m["done"]]
                break
            # nothing arriving for five minutes: stuck (or finished without some mods)
            if STATE["bytes"] != last_bytes:
                last_bytes, last_change = STATE["bytes"], time.time()
            elif time.time() - last_change > 300:
                STATE["failed"] = [m["guid"] for m in STATE["mods"] if not m["done"]]
                STATE["error"] = "No progress for 5 minutes; %d mod(s) not downloaded." % len(STATE["failed"])
                break
            if time.time() - STATE["started_at"] > 4 * 3600:
                STATE["error"] = "Gave up after 4 hours."
                break
    except Exception as e:
        STATE["error"] = str(e)
    finally:
        stop()
        STATE.update(running=False, finished_at=time.time())
        log("[download] ended:", STATE["state"], STATE["error"])
        if PENDING:
            nxt = PENDING[:]
            del PENDING[:]
            threading.Thread(target=start, args=(nxt,), daemon=True).start()


def stop():
    p = _proc[0]
    _proc[0] = None
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=15)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    return {"ok": True}


def status():
    s = dict(STATE)
    s["installed"] = bool(server_exe())
    return s
