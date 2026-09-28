"""Party: play together over Radmin (or any LAN).

The host's app shares, on the party port, what the party plays (mods in load
order, scenario) and the files of those mods. Members' apps poll the host,
compare the host's mods with their own, download what is missing or different
straight from the host, and start their game with -client <host>:<game port>
when the host launches.

Read-only for members: they can only fetch files of mods that are in the
current party setup.
"""
import getpass
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import play

log = print
CFG = {}
HERE = os.path.dirname(os.path.abspath(__file__))
PARTY_PATH = os.path.join(HERE, "party_settings.json")


def init(config, logger):
    global CFG, log
    CFG = config
    log = logger


def party_port():
    return int(CFG.get("party_port") or 8766)


def game_port():
    return int(CFG.get("game_port") or 2001)


# ----------------------------------------------------------------------------
# small persistent settings: your name, last host address
# ----------------------------------------------------------------------------
def prefs():
    try:
        with open(PARTY_PATH, "r", encoding="utf-8") as f:
            p = json.load(f)
    except Exception:
        p = {}
    if not p.get("name"):
        try:
            p["name"] = getpass.getuser()
        except Exception:
            p["name"] = "Player"
    p.setdefault("last_host", "")
    p.setdefault("auto_join", True)
    if not p.get("id"):
        # stable id so renaming yourself does not show up as a second player
        import uuid
        p["id"] = uuid.uuid4().hex[:12]
        try:
            prefs_put(p)
        except Exception:
            pass
    return p


def prefs_put(p):
    tmp = PARTY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, PARTY_PATH)


def my_addresses():
    """This PC's IPv4 addresses, Radmin's (26.x) first."""
    ips = set()
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(sa[0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips, key=lambda ip: (not ip.startswith("26."), ip))


# ----------------------------------------------------------------------------
# fingerprints: how two PCs tell whether they have the same mod
# ----------------------------------------------------------------------------
HASH_PATH = os.path.join(HERE, "party_hashes.json")
_hash_cache = {}        # "path|size|mtime" -> sha1; kept on disk so a big map is hashed once, ever
_hash_dirty = [False]
_fp_cache = {}          # folder -> (time, fingerprint), so a party tick does not re-walk a map every 3 s
FP_TTL = 15


def _load_hashes():
    try:
        with open(HASH_PATH, "r", encoding="utf-8") as f:
            _hash_cache.update(json.load(f))
    except Exception:
        pass


def _save_hashes():
    if not _hash_dirty[0]:
        return
    try:
        # drop entries for files that no longer exist, so the file does not grow forever
        keep = {k: v for k, v in _hash_cache.items() if os.path.exists(k.rsplit("|", 2)[0])}
        tmp = HASH_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f)
        os.replace(tmp, HASH_PATH)
        _hash_dirty[0] = False
    except Exception as e:
        log("[party] could not save hash cache", e)


def _sha1(path):
    st = os.stat(path)
    key = "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))
    if key in _hash_cache:
        return _hash_cache[key]
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    _hash_cache[key] = h.hexdigest()
    _hash_dirty[0] = True
    return _hash_cache[key]


_load_hashes()


def load_folder(mod):
    """The folder the game actually loads for this mod."""
    if mod.get("kind") == "local" and mod.get("build") == "packed" and mod.get("build_path"):
        return mod["build_path"]
    return mod["path"]


def file_list(folder, with_hash):
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in files:
            rel = os.path.relpath(os.path.join(root, n), folder).replace("\\", "/")
            if rel.endswith((".tmp", ".partydl")) or rel.startswith("_"):
                continue
            p = os.path.join(root, n)
            try:
                e = {"name": rel, "size": os.path.getsize(p)}
            except OSError:
                continue
            if with_hash:
                e["sha1"] = _sha1(p)
            out.append(e)
    out.sort(key=lambda e: e["name"])
    return out


def fingerprint(mod):
    """Workshop mods: their Workshop version plus data.pak size (hashing gigabytes of RHS every
    time would be slow, and the version already names the exact upload). Local mods: a hash
    over every file, because they change without a version bump."""
    folder = load_folder(mod)
    if mod.get("kind") == "workshop":
        try:
            size = os.path.getsize(os.path.join(folder, "data.pak"))
        except OSError:
            size = -1
        return "ws:%s:%d" % (mod.get("version") or "", size)
    hit = _fp_cache.get(folder)
    if hit and time.time() - hit[0] < FP_TTL:
        return hit[1]
    h = hashlib.sha1()
    for e in file_list(folder, True):
        h.update(("%s:%d:%s\n" % (e["name"], e["size"], e["sha1"])).encode())
    fp = "local:" + h.hexdigest()[:20]
    _fp_cache[folder] = (time.time(), fp)
    _save_hashes()
    return fp


# ----------------------------------------------------------------------------
# host side
# ----------------------------------------------------------------------------
HOST = {"on": False, "server": None, "manifest": None, "members": {}, "launched_at": 0, "started_at": 0, "ready_at": 0}
_host_lock = threading.Lock()


def build_manifest():
    _fp_cache.clear()     # the host's view must be exact (a rebuild may have just happened)
    st = play.settings_get()
    mods = play.scan_mods()
    order, added, missing = play.resolve([g.upper() for g in st["mods"]], mods)
    items = []
    for g in order:
        m = mods[g]
        folder = load_folder(m)
        files = file_list(folder, False)
        items.append({"guid": g, "name": m["name"], "kind": m["kind"], "version": m.get("version") or "",
                      "folder": os.path.basename(folder.rstrip("\\/")), "fp": fingerprint(m),
                      "size": sum(f["size"] for f in files), "files": files})
    scen_label = ""
    try:
        for s in play.scenarios([g for g in st["mods"]])["scenarios"] + play.vanilla_scenarios():
            if s["rid"] == st["scenario"]:
                scen_label = s["label"]
                break
    except Exception:
        pass
    return {"host": prefs()["name"], "order": order, "missing": missing, "mods": items,
            "scenario": st["scenario"], "scenario_label": scen_label, "load_save": st["load_save"],
            "game_port": game_port(), "built_at": time.time()}


def host_start():
    with _host_lock:
        if HOST["on"]:
            return host_status()
        man = build_manifest()
        if not man["order"] or not man["scenario"]:
            return {"error": "Pick your mods and a scenario first; that is what the party plays."}
        if not share_start():
            return {"error": "Could not open party port %d (another program is using it?)" % party_port()}
        HOST.update(on=True, manifest=man, members={}, launched_at=0, started_at=time.time(), ready_at=0)
        log("[party] hosting on port", party_port(), "addresses", my_addresses())
    return host_status()


def host_refresh():
    """The host changed mods or scenario: members see the new setup on their next poll."""
    if HOST["on"]:
        HOST["manifest"] = build_manifest()


def host_stop():
    with _host_lock:
        HOST.update(on=False, manifest=None, members={}, launched_at=0, ready_at=0)
    return {"ok": True}


_share = [None]


def share_start():
    """One read-only server on the party port for as long as the browser is open: app
    updates for friends always, party info and mod files only while a party runs."""
    if _share[0]:
        return True
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", party_port()), ShareHandler)
    except OSError as e:
        log("[share] could not open port", party_port(), e)
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _share[0] = srv
    return True


def host_status():
    now = time.time()
    members = []
    for key, m in list(HOST["members"].items()):
        if now - m["seen"] > 20:
            continue
        members.append(dict(m, name=m.get("name") or key, id=m.get("id") or key))
    # the host shown alongside everyone else (not counted when checking who is ready)
    pr = prefs()
    tot = len((HOST["manifest"] or {}).get("mods") or [])
    me = {"name": pr["name"], "id": pr["id"], "host": True, "synced": tot, "total": tot, "ready": True,
          "in_game": bool(_game_running[0]()), "syncing": False, "problems": []}
    return {"on": HOST["on"], "addresses": my_addresses(), "party_port": party_port(), "game_port": game_port(),
            "manifest": _slim(HOST["manifest"]), "members": members, "everyone": [me] + members,
            "launched_at": HOST["launched_at"], "ready_at": HOST["ready_at"]}


def _slim(man):
    if not man:
        return None
    return dict(man, mods=[{k: v for k, v in m.items() if k != "files"} for m in man["mods"]])


def not_ready_members():
    """Members who are here (heard from in the last 20 s) but do not have every mod yet."""
    return [m for m in host_status()["members"] if not (m["total"] and m["synced"] == m["total"])]


def host_launch(game_running):
    if not HOST["on"]:
        return {"ok": False, "error": "No party running."}
    host_refresh()
    waiting = not_ready_members()
    if waiting:
        return {"ok": False, "waiting": [m["name"] for m in waiting],
                "error": "Waiting for mods: " + ", ".join("%s (%d of %d)" % (m["name"], m["synced"], m["total"]) for m in waiting)}
    st = play.settings_get()
    extra = (st.get("extra_args") or "")
    if "-bindPort" not in extra:
        extra += " -bindIP 0.0.0.0 -bindPort %d" % game_port()
    r = play.launch([g.upper() for g in st["mods"]], st["scenario"], st["load_save"], extra.strip(), game_running)
    if r.get("ok"):
        HOST["launched_at"] = time.time()
        HOST["ready_at"] = 0
        threading.Thread(target=_watch_host_ready, args=(HOST["launched_at"],), daemon=True).start()
    return r


# the game log line that means the host's game is up and taking players
READY_LINES = ("Starting RPL server, listening on", "### Creating player: PlayerId=1,")


def _watch_host_ready(launched):
    """Follow the host game's console.log from this launch and mark the party ready the moment the
    game starts listening for players, so friends join right then instead of after a fixed wait.
    Gives up after 4 minutes and marks it ready anyway (a friend's game retries the connect)."""
    logs = os.path.join(os.path.dirname(play.addons_dir()), "logs")
    path, pos, buf = None, 0, ""
    while HOST["launched_at"] == launched and time.time() - launched < 240:
        try:
            if not path:
                ds = [os.path.join(logs, d) for d in os.listdir(logs) if d.startswith("logs_")]
                ds = [d for d in ds if os.path.getctime(d) >= launched - 10 and os.path.isfile(os.path.join(d, "console.log"))]
                if ds:
                    path = os.path.join(max(ds, key=os.path.getctime), "console.log")
            if path:
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos += len(chunk)
                buf = (buf + chunk.decode("utf-8", "replace"))[-4000:]
                if any(k in buf for k in READY_LINES):
                    break
        except Exception:
            pass
        time.sleep(1)
    if HOST["launched_at"] == launched:
        HOST["ready_at"] = time.time()


class ShareHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.startswith("/app/"):
            import update
            if update.serve(self, u.path, q, prefs().get("name") or ""):
                return
        man = HOST.get("manifest")
        if not HOST["on"] or not man:
            return self._json(404, {"error": "no party"})
        if u.path == "/party/info":
            return self._json(200, {"manifest": man, "launched_at": HOST["launched_at"], "ready_at": HOST["ready_at"],
                                    "members": host_status()["everyone"]})
        if u.path == "/party/file":
            guid, name = (q.get("guid") or "").upper(), q.get("name") or ""
            mod = next((m for m in man["mods"] if m["guid"] == guid), None)
            if not mod or not any(f["name"] == name for f in mod["files"]):
                return self._json(403, {"error": "not shared"})
            mods = play.scan_mods()
            if guid not in mods:
                return self._json(404, {"error": "gone"})
            path = os.path.join(load_folder(mods[guid]), *name.split("/"))
            try:
                size = os.path.getsize(path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        self.wfile.write(chunk)
            except (OSError, ConnectionError):
                pass
            return
        return self._json(404, {"error": "no such route"})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}
        if u.path == "/party/hello" and HOST["on"]:
            name = str(body.get("name") or "Player")[:40]
            key = str(body.get("id") or name)[:40]
            HOST["members"][key] = {"name": name, "id": key, "ip": self.client_address[0], "seen": time.time(),
                                     "synced": int(body.get("synced") or 0), "total": int(body.get("total") or 0),
                                     "problems": body.get("problems") or [], "ready": bool(body.get("ready")),
                                     "in_game": bool(body.get("in_game")), "syncing": bool(body.get("syncing"))}
            return self._json(200, {"ok": True, "launched_at": HOST["launched_at"], "built_at": HOST["manifest"]["built_at"]})
        return self._json(404, {"error": "no such route"})


# ----------------------------------------------------------------------------
# member side
# ----------------------------------------------------------------------------
MEMBER = {"on": False, "host": "", "info": None, "error": "", "report": [], "ready": False,
          "sync": {"running": False, "mod": "", "done": 0, "total": 0, "error": ""},
          "joined_launch": 0, "last_ok": 0, "ended": ""}
_member_lock = threading.Lock()


def _url(path):
    host = MEMBER["host"]
    if ":" not in host:
        host = "%s:%d" % (host, party_port())
    return "http://%s%s" % (host, path)


def member_join(host, name):
    host = (host or "").strip()
    if not host:
        return {"error": "Enter the host's Radmin address."}
    p = prefs()
    p["last_host"] = host
    if name:
        p["name"] = name.strip()[:40]
    prefs_put(p)
    MEMBER.update(on=True, host=host, info=None, error="", ready=False, joined_launch=0, last_ok=0, ended="")
    if not getattr(member_join, "_thread", None):
        member_join._thread = threading.Thread(target=_member_loop, daemon=True)
        member_join._thread.start()
    _member_tick()
    return member_status()


def member_leave():
    MEMBER.update(on=False, info=None, error="", ready=False, report=[], ended="")
    return {"ok": True}


def _host_name():
    man = (MEMBER.get("info") or {}).get("manifest") or {}
    return man.get("host") or "The host"


def _party_ended(msg):
    """Downloads already running keep going (the files are still useful); nothing is deleted."""
    log("[party]", msg)
    MEMBER.update(on=False, info=None, error="", ready=False, report=[], ended=msg, last_ok=0)


def member_ready(v):
    MEMBER["ready"] = bool(v)
    return member_status()


_game_running = [lambda: False]


def set_game_running(fn):
    _game_running[0] = fn


def _member_loop():
    while True:
        time.sleep(3)
        if MEMBER["on"]:
            try:
                _member_tick()
            except Exception as e:
                MEMBER["error"] = str(e)


def _member_tick():
    try:
        with urllib.request.urlopen(_url("/party/info"), timeout=5) as r:
            info = json.loads(r.read())
    except Exception as e:
        # the host stopped hosting, or closed the browser (which ends the party): leave it
        if getattr(e, "code", None) == 404 and MEMBER["last_ok"]:
            _party_ended("%s ended the party." % _host_name())
            return
        if MEMBER["last_ok"] and time.time() - MEMBER["last_ok"] > 20:
            _party_ended("Lost %s for 20 seconds (their browser closed or Radmin dropped). You left the party; join again when they host." % _host_name())
            return
        MEMBER["error"] = "Can't reach the host (%s). Check the address, that you are both on the Radmin network, and that the host started a party." % e
        return
    MEMBER["info"] = info
    MEMBER["error"] = ""
    MEMBER["last_ok"] = time.time()
    rep = compare(info["manifest"])
    MEMBER["report"] = rep
    # host removed mods: don't bother downloading those any more (never deletes anything on disk)
    import wsdl
    wanted = {m["guid"] for m in info["manifest"]["mods"]}
    with wsdl._lock:
        wsdl.PENDING[:] = [m for m in wsdl.PENDING if m["guid"] in wanted]
    bad = [r for r in rep if r["state"] != "ok"]
    if bad:
        _auto_fetch(bad)
    import wsdl
    busy = MEMBER["sync"]["running"] or wsdl.STATE["running"]
    MEMBER["ready"] = not bad
    body = {"name": prefs()["name"], "id": prefs()["id"], "synced": len(rep) - len(bad), "total": len(rep),
            "problems": ["%s (%s)" % (r["name"], r["state"]) for r in bad][:8],
            "ready": not bad, "in_game": _game_running[0](), "syncing": busy}
    try:
        req = urllib.request.Request(_url("/party/hello"), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception:
        pass
    # host launched: join when synced, ready and auto_join is on, once per launch, after the host
    # has had time to load the scenario
    la = info.get("launched_at") or 0
    if la and la != MEMBER["joined_launch"] and not bad and prefs().get("auto_join", True):
        if "ready_at" in info:   # host reports when its game is up
            go = (info.get("ready_at") or 0) >= la
        else:                    # older host: fixed wait
            go = time.time() - la >= int(CFG.get("party_join_delay") or 75)
        if go and not _game_running[0]():
            MEMBER["joined_launch"] = la
            r = member_launch()
            MEMBER["launch_error"] = "" if r.get("ok") else (r.get("error") or "Could not start the game.")   # shown until the next try


_fetch_tries = {}   # what -> last attempt time, so a failure is retried once a minute, not every tick


def _auto_fetch(bad):
    """Get missing or different mods without anyone clicking anything: the host's own mods (and,
    without the server tool, everything) are copied from the host; Workshop mods come from
    Bohemia through the hidden downloader."""
    import wsdl
    now = time.time()
    local = [r["guid"] for r in bad if r["kind"] != "workshop"]
    ws = [r for r in bad if r["kind"] == "workshop"]
    have_tool = bool(wsdl.server_exe())
    from_host = local + ([] if have_tool else [r["guid"] for r in ws])
    if from_host and not MEMBER["sync"]["running"] and now - _fetch_tries.get("host", 0) > 60:
        _fetch_tries["host"] = now
        member_sync(from_host)
    if ws and have_tool:
        if wsdl.STATE["running"]:
            # host added mods mid-download: queue them right away (start() skips ones already queued)
            ws_start()
        elif now - _fetch_tries.get("ws", 0) > 60:
            _fetch_tries["ws"] = now
            ws_start()


def compare(man):
    mine = play.scan_mods()
    out = []
    for m in man["mods"]:
        have = mine.get(m["guid"])
        if not have:
            state = "missing"
        else:
            state = "ok" if fingerprint(have) == m["fp"] else "different"
        out.append({"guid": m["guid"], "name": m["name"], "kind": m["kind"], "size": m["size"],
                    "state": state, "host_version": m.get("version") or "",
                    "my_version": (have or {}).get("version") or ""})
    return out


def member_status():
    info = MEMBER.get("info") or {}
    man = info.get("manifest")
    return {"on": MEMBER["on"], "host": MEMBER["host"], "error": MEMBER["error"] or MEMBER.get("launch_error") or play.START["error"] or "", "ready": MEMBER["ready"], "ended": MEMBER.get("ended") or "",
            "manifest": _slim(man), "report": MEMBER["report"], "sync": MEMBER["sync"],
            "launched_at": info.get("launched_at") or 0, "members": info.get("members") or [],
            "host_ready": ("ready_at" not in info) or (info.get("ready_at") or 0) >= (info.get("launched_at") or 1),
            "prefs": prefs(), "addresses": my_addresses()}


def member_sync(guids=None):
    if MEMBER["sync"]["running"]:
        return {"error": "Already syncing."}
    man = (MEMBER.get("info") or {}).get("manifest")
    if not man:
        return {"error": "Not connected to a party."}
    todo = [r["guid"] for r in compare(man) if r["state"] != "ok" and (not guids or r["guid"] in guids)]
    if not todo:
        return {"ok": True, "nothing": True}
    mods = [m for m in man["mods"] if m["guid"] in todo]
    MEMBER["sync"] = {"running": True, "mod": "", "done": 0, "total": sum(m["size"] for m in mods), "error": "",
                      "queue": [{"guid": m["guid"], "name": m["name"], "size": m["size"]} for m in mods], "finished": []}
    threading.Thread(target=_sync, args=(mods,), daemon=True).start()
    return {"ok": True}


def _sync(mods):
    try:
        for m in mods:
            MEMBER["sync"]["mod"] = m["name"]
            MEMBER["sync"]["mod_guid"] = m["guid"]
            MEMBER["sync"]["mod_size"] = m["size"]
            MEMBER["sync"]["mod_start"] = MEMBER["sync"]["done"]
            # Workshop mods go where the game keeps its downloads, named the way the game names them;
            # local mods go to localmods. Each is assembled in a temp folder and swapped in whole.
            if m["kind"] == "workshop":
                root = play.addons_dir()
                folder = m["folder"]
            else:
                root = play.local_mods_dir()
                folder = m["folder"]
            os.makedirs(root, exist_ok=True)
            dst = os.path.join(root, folder)
            tmp = dst + ".partydl"
            if os.path.isdir(tmp):
                import shutil
                shutil.rmtree(tmp)
            for f in m["files"]:
                target = os.path.join(tmp, *f["name"].split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                q = urllib.parse.urlencode({"guid": m["guid"], "name": f["name"]})
                with urllib.request.urlopen(_url("/party/file?" + q), timeout=30) as r, open(target, "wb") as out:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        MEMBER["sync"]["done"] += len(chunk)
                if os.path.getsize(target) != f["size"]:
                    raise RuntimeError("%s: %s came through incomplete" % (m["name"], f["name"]))
            import shutil
            # remove any other copy of this GUID in localmods so the game never sees two
            play._drop_other_copies(m["guid"], keep=dst)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            os.replace(tmp, dst)
            import wsdl
            wsdl.HISTORY.insert(0, {"guid": m["guid"], "name": m["name"], "size": m["size"], "at": time.time(),
                                    "thumb": "", "source": "from host"})
            MEMBER["sync"].setdefault("finished", []).append(m["guid"])
            log("[party] synced", m["name"], "->", dst)
    except Exception as e:
        MEMBER["sync"]["error"] = str(e)
        log("[party] sync failed", e)
    finally:
        MEMBER["sync"]["running"] = False
        MEMBER["sync"]["mod"] = ""
        _fp_cache.clear()     # copied files: check them fresh
        play.invalidate_scan()


def member_launch():
    man = (MEMBER.get("info") or {}).get("manifest")
    if not man:
        return {"ok": False, "error": "Not connected to a party."}
    if _game_running[0]():
        return {"ok": False, "error": "Arma Reforger is already running."}
    bad = [r for r in compare(man) if r["state"] != "ok"]
    if bad:
        return {"ok": False, "error": "Not synced yet: " + ", ".join(r["name"] for r in bad)}
    g = play.game_dir()
    if not g:
        return {"ok": False, "error": "Arma Reforger install not found. Set game_dir in config.json."}
    host = MEMBER["host"].split(":")[0]
    args = [os.path.join(g, "ArmaReforgerSteam.exe"), "-addonsDir", ",".join([play.local_mods_dir(), play.addons_dir()]),
            "-addons", ",".join(man["order"]), "-client", "%s:%d" % (host, int(man.get("game_port") or 2001))]
    MEMBER["launch_error"] = ""
    err = play.ensure_steam()
    if err:
        MEMBER["launch_error"] = err
        return {"ok": False, "error": err}
    log("[party] joining:", subprocess.list2cmdline(args))
    try:
        play.start_game(args, g)
    except Exception as e:
        return {"ok": False, "error": "Could not start the game: %s" % e}
    return {"ok": True, "command": subprocess.list2cmdline(args)}


# ----------------------------------------------------------------------------
# Workshop mods through the game's own downloader (Better Workshop companion)
# ----------------------------------------------------------------------------
COMPANION_GUID = "5B7E4C2A9D1F0B36"
WS = {"running": False, "stamp": "", "state": "", "progress": 0.0, "downloads": 0, "done": 0,
      "failed": [], "error": "", "started_at": 0, "we_started_game": False}


def companion_dir():
    """Folder holding the companion mod, loose. Shipped next to the app for friends; on the
    developer's PC it is refreshed from the Workbench project."""
    import shutil
    root = os.path.join(HERE, "companion")
    dst = os.path.join(root, "BetterWorkshop")
    src = os.path.join(play.workbench_addons_dir(), "BetterWorkshop")
    if os.path.isdir(src):
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "*.pak"))
    return root if os.path.isdir(dst) else ""


def queue_dir():
    return CFG.get("queue_dir") or os.path.join(os.path.expanduser("~"), "Documents", "My Games", "ArmaReforger", "profile", "BetterWorkshop")


def ws_start():
    """Workshop mods for the party, downloaded without the game (hidden server helper),
    pinned to the host's exact versions."""
    import wsdl
    man = (MEMBER.get("info") or {}).get("manifest")
    if not man:
        return {"error": "Not connected to a party."}
    todo = [r for r in compare(man) if r["state"] != "ok" and r["kind"] == "workshop"]
    if not todo:
        return {"ok": True, "nothing": True}
    r = wsdl.start([{"guid": x["guid"], "name": x["name"], "version": x["host_version"], "size": x["size"]} for x in todo])
    if not r.get("ok"):
        return dict(r, error=r.get("error"))
    return r


def ws_start_ingame():
    if WS["running"]:
        return {"error": "Already downloading."}
    man = (MEMBER.get("info") or {}).get("manifest")
    if not man:
        return {"error": "Not connected to a party."}
    todo = [r for r in compare(man) if r["state"] != "ok" and r["kind"] == "workshop"]
    if not todo:
        return {"ok": True, "nothing": True}
    if _game_running[0]():
        return {"error": "Close Arma Reforger first; the download runs the game in the background."}
    cdir = companion_dir()
    if not cdir:
        return {"error": "The companion mod (companion\\BetterWorkshop) is missing from the app folder."}
    g = play.game_dir()
    if not g:
        return {"error": "Arma Reforger install not found. Set game_dir in config.json."}
    stamp = "party%d" % int(time.time())
    lines = ["playlist=party download", "written=" + stamp, "mode=download"] + ["+%s %s" % (r["guid"], r["name"]) for r in todo]
    qd = queue_dir()
    os.makedirs(qd, exist_ok=True)
    tmp = os.path.join(qd, "queue.txt.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(qd, "queue.txt"))
    WS.update(running=True, stamp=stamp, state="starting the game", progress=0.0, downloads=len(todo), done=0,
              failed=[], error="", started_at=time.time(), we_started_game=True)
    args = [os.path.join(g, "ArmaReforgerSteam.exe"), "-addonsDir", cdir, "-addons", COMPANION_GUID]
    log("[party] workshop download:", subprocess.list2cmdline(args), len(todo), "mods")
    try:
        play.start_game(args, g)
    except Exception as e:
        WS.update(running=False, error="Could not start the game: %s" % e)
        return {"error": WS["error"]}
    threading.Thread(target=_ws_watch, daemon=True).start()
    return {"ok": True}


def _read_status():
    out = {}
    try:
        with open(os.path.join(queue_dir(), "status.txt"), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == "failed":
                    out.setdefault("failed", []).append(v)
                else:
                    out[k] = v
    except OSError:
        pass
    return out


def _ws_watch():
    try:
        while WS["running"]:
            time.sleep(2)
            st = _read_status()
            if st.get("stamp") == WS["stamp"]:
                WS["state"] = st.get("state") or ""
                try:
                    WS["progress"] = float(st.get("download_progress") or 0)
                    WS["done"] = int(st.get("downloads_done") or 0)
                except ValueError:
                    pass
                WS["failed"] = st.get("failed") or []
                if st.get("state") == "done":
                    time.sleep(3)
                    _close_game()
                    WS["state"] = "done"
                    break
            elif time.time() - WS["started_at"] > 30:
                WS["state"] = "waiting for the game to reach the main menu"
            if time.time() - WS["started_at"] > 60 and not _game_running[0]():
                WS["error"] = "The game closed before the download finished."
                break
            if time.time() - WS["started_at"] > 3 * 3600:
                WS["error"] = "Gave up after 3 hours."
                break
    finally:
        WS["running"] = False


def _close_game():
    if os.name != "nt":
        return
    try:
        subprocess.run(["taskkill", "/IM", "ArmaReforgerSteam.exe"], capture_output=True, timeout=15, creationflags=0x08000000)
        time.sleep(8)
        if _game_running[0]():
            subprocess.run(["taskkill", "/F", "/IM", "ArmaReforgerSteam.exe"], capture_output=True, timeout=15, creationflags=0x08000000)
    except Exception as e:
        log("[party] could not close the game", e)


def ws_status():
    import wsdl
    return wsdl.status()



def downloads_view():
    """Everything downloading, queued and finished, for the Downloads tab."""
    import wsdl
    active, queued = [], []
    w = wsdl.STATE
    if w["running"]:
        for m in w["mods"]:
            if m["done"] or (m.get("check") and not m.get("bytes")):
                continue    # an update check lists every mod; only show what is actually downloading
            row = {"guid": m["guid"], "name": m.get("name") or m["guid"], "source": "Workshop", "thumb": m.get("thumb") or "",
                   "bytes": m.get("bytes") or 0, "size": int(m.get("size") or 0)}
            (active if m.get("bytes") else queued).append(row)
    for m in wsdl.PENDING:
        queued.append({"guid": m["guid"], "name": m.get("name") or m["guid"], "source": "Workshop",
                       "thumb": m.get("thumb") or "", "bytes": 0, "size": int(m.get("size") or 0)})
    s = MEMBER["sync"]
    if s.get("running"):
        cur = s.get("mod_guid")
        for q in s.get("queue") or []:
            if q["guid"] in (s.get("finished") or []):
                continue
            row = {"guid": q["guid"], "name": q["name"], "source": "from host", "thumb": "", "size": q["size"],
                   "bytes": (s["done"] - s.get("mod_start", 0)) if q["guid"] == cur else 0}
            (active if q["guid"] == cur else queued).append(row)
    speed = (w.get("speed") or 0) if w["running"] else 0
    return {"active": active, "queued": queued, "history": wsdl.HISTORY[:50], "speed": speed,
            "workshop": {k: w[k] for k in ("running", "error", "state", "finished_at", "done", "total")},
            "installed": bool(wsdl.server_exe()), "sync_error": s.get("error") or ""}
