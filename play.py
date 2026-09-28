"""Play tab: pick mods (Workshop downloads and local Workbench projects), pick a
scenario, and start Arma Reforger straight into a locally hosted session.

The game is started directly (not through Steam) with
    -addonsDir <local mods>,<addons> -addons <GUIDs in load order>
    -server {GUID}Missions/xyz.conf [-loadSessionSave]
which skips the main menu and the Workshop availability check, so local mods
load like any other mod.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import zlib

BASE_GUIDS = {"58D0FB3206B6F859", "5614BBCCBB55ED1C"}   # ArmaReforger data, core
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "play_cache.json")
SETTINGS_PATH = os.path.join(HERE, "play_settings.json")

CFG = {}
log = print


def init(config, logger):
    global CFG, log
    CFG = config
    log = logger


# ----------------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------------
def _home(*p):
    return os.path.join(os.path.expanduser("~"), *p)


def addons_dir():
    return CFG.get("addons_dir") or _home("Documents", "My Games", "ArmaReforger", "addons")


def local_mods_dir():
    return CFG.get("local_mods_dir") or _home("Documents", "My Games", "ArmaReforger", "localmods")


def workbench_addons_dir():
    return CFG.get("workbench_addons_dir") or _home("Documents", "My Games", "ArmaReforgerWorkbench", "addons")


def steam_libraries():
    libs = []
    steam = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except Exception:
        pass
    for s in [steam, r"C:\Program Files (x86)\Steam"]:
        if not s:
            continue
        vdf = os.path.join(s, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
        libs.append(s)
    for d in "CDEFGB":
        libs.append(d + r":\SteamLibrary")
    out = []
    for l in libs:
        if l and l not in out:
            out.append(l)
    return out


def game_dir():
    g = CFG.get("game_dir")
    if g and os.path.exists(os.path.join(g, "ArmaReforgerSteam.exe")):
        return g
    for lib in steam_libraries():
        g = os.path.join(lib, "steamapps", "common", "Arma Reforger")
        if os.path.exists(os.path.join(g, "ArmaReforgerSteam.exe")):
            return g
    return ""


def workbench_exe():
    w = CFG.get("workbench_exe")
    if w and os.path.exists(w):
        return w
    for lib in steam_libraries():
        d = os.path.join(lib, "steamapps", "common", "Arma Reforger Tools", "Workbench")
        for n in ("ArmaReforgerWorkbenchSteam.exe", "ArmaReforgerWorkbenchSteamDiag.exe"):
            if os.path.exists(os.path.join(d, n)):
                return os.path.join(d, n)
    return ""


# ----------------------------------------------------------------------------
# reading mods
# ----------------------------------------------------------------------------
def read_gproj(folder):
    """GUID, ID, TITLE and dependency GUIDs from the folder's .gproj."""
    try:
        names = [n for n in os.listdir(folder) if n.lower().endswith(".gproj")]
    except OSError:
        return None
    if not names:
        return None
    names.sort(key=lambda n: (n.lower() != "addon.gproj", n))
    try:
        with open(os.path.join(folder, names[0]), "r", encoding="utf-8-sig", errors="replace") as f:
            t = f.read()
    except OSError:
        return None
    g = re.search(r'\bGUID\s+"([0-9A-Fa-f]{16})"', t)
    if not g:
        return None
    title = re.search(r'\bTITLE\s+"([^"]*)"', t)
    pid = re.search(r'\bID\s+"([^"]*)"', t)
    deps = []
    m = re.search(r"\bDependencies\s*\{([^}]*)\}", t)
    if m:
        deps = [d.upper() for d in re.findall(r'"([0-9A-Fa-f]{16})"', m.group(1))]
    return {"guid": g.group(1).upper(), "title": title.group(1) if title else "",
            "pid": pid.group(1) if pid else "", "deps": [d for d in deps if d not in BASE_GUIDS],
            "gproj": names[0]}


def files_complete(folder):
    """True when every file that has a <file>_<version>_manifest.json next to it has the size the
    manifest says. A download in progress (or cut short) has a data.pak of the wrong size."""
    found = False
    for n in _listdir(folder):
        m = re.match(r"(.+)_[^_]+_manifest\.json$", n)
        if not m:
            continue
        try:
            with open(os.path.join(folder, n), "r", encoding="utf-8") as f:
                want = int(json.load(f).get("size") or -1)
            have = os.path.getsize(os.path.join(folder, m.group(1)))
        except (OSError, ValueError):
            return False
        if want >= 0 and have != want:
            return False
        found = True
    # folders without manifests (hand-made packs) count as complete when data.pak is not empty
    if not found:
        try:
            return os.path.getsize(os.path.join(folder, "data.pak")) > 0
        except OSError:
            return False
    return True


def _meta_name(folder):
    try:
        with open(os.path.join(folder, "meta"), "r", encoding="utf-8-sig") as f:
            m = json.load(f).get("meta") or {}
        return m.get("name") or "", (m.get("versions") or [{}])[0].get("version") or ""
    except Exception:
        return "", ""


def _newest_mtime(folder, limit=4000):
    newest, n = 0, 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".meta") or f == "resourceDatabase.rdb":
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
            n += 1
            if n > limit:
                return newest
    return newest


_scan_cache = {"at": 0, "mods": None}
SCAN_TTL = 8


def invalidate_scan():
    """Something changed on disk (a download, a build, a sync): the next scan reads fresh."""
    _scan_cache["at"] = 0


def scan_mods():
    """Cached for a few seconds: the Play tab, the party checks and the downloader all ask for it,
    and one scan reads every mod folder."""
    if _scan_cache["mods"] is not None and time.time() - _scan_cache["at"] < SCAN_TTL:
        return {g: dict(m) for g, m in _scan_cache["mods"].items()}
    mods = _scan_mods()
    _scan_cache.update(at=time.time(), mods=mods)
    return {g: dict(m) for g, m in mods.items()}


def _scan_mods():
    """Every mod the game could load, one entry per GUID.

    kind: workshop  downloaded by the game (addons folder)
          local     your Workbench project; 'build' says how it will load:
                    packed (a data.pak build in localmods) or loose (source copy)
    """
    mods = {}
    # downloaded workshop mods
    ad = addons_dir()
    for name in _listdir(ad):
        f = os.path.join(ad, name)
        if not os.path.exists(os.path.join(f, "data.pak")):
            continue
        if not files_complete(f):
            continue    # half downloaded: not installed as far as the game is concerned
        g = read_gproj(f)
        if not g:
            continue
        mname, ver = _meta_name(f)
        mods[g["guid"]] = dict(g, name=mname or g["title"] or name, version=ver, kind="workshop", path=f,
                               folder=name, build="packed")
    # packed local builds already in localmods
    ld = local_mods_dir()
    packed_local = {}
    for name in _listdir(ld):
        f = os.path.join(ld, name)
        g = read_gproj(f)
        if g and os.path.exists(os.path.join(f, "data.pak")):
            packed_local[g["guid"]] = (f, os.path.getmtime(os.path.join(f, "data.pak")))
    # Workbench projects (source folders, no data.pak)
    wd = workbench_addons_dir()
    for name in sorted(_listdir(wd), key=lambda n: (" - copy" in n.lower(), n.lower())):
        f = os.path.join(wd, name)
        if os.path.exists(os.path.join(f, "data.pak")):
            continue    # a workshop download the Workbench keeps for itself
        g = read_gproj(f)
        if not g or g["guid"] in BASE_GUIDS:
            continue
        if g["guid"] in mods:
            if mods[g["guid"]]["kind"] == "local":
                continue    # duplicate copy of a project; first (non-Copy) wins
            continue        # same GUID as a Workshop download; the download wins
        entry = dict(g, name=g["title"] or g["pid"] or name, version="", kind="local", path=f, folder=name)
        if g["guid"] in packed_local:
            pf, pt = packed_local[g["guid"]]
            entry.update(build="packed", build_path=pf, built_at=pt,
                         stale=_newest_mtime(f) > pt + 2)
        else:
            entry.update(build="loose")
        mods[g["guid"]] = entry
    # packed local builds whose source is gone
    for guid, (pf, pt) in packed_local.items():
        if guid not in mods:
            g = read_gproj(pf)
            mods[guid] = dict(g, name=g["title"] or os.path.basename(pf), version="", kind="local", path=pf,
                              folder=os.path.basename(pf), build="packed", build_path=pf, built_at=pt, stale=False)
    return mods


def local_guids():
    """Every GUID that is one of YOUR mods: any project in the Workbench addons folder or
    anything in localmods, whatever else shares the GUID. The browser never deletes these."""
    out = set()
    for root in (workbench_addons_dir(), local_mods_dir()):
        for n in _listdir(root):
            f = os.path.join(root, n)
            if os.path.exists(os.path.join(f, "data.pak")) and root == workbench_addons_dir():
                continue    # a Workshop download the Workbench keeps, not a project
            g = read_gproj(f)
            if g:
                out.add(g["guid"])
    for extra in CFG.get("local_mods_src") or []:
        g = read_gproj(extra)
        if g:
            out.add(g["guid"])
    return out


def _listdir(d):
    """Folder names, minus half-finished party downloads."""
    try:
        return [n for n in os.listdir(d) if not n.endswith(".partydl")]
    except OSError:
        return []


# ----------------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------------
_cache_lock = threading.Lock()


def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(c):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        log("[play] cache write failed", e)


def _rdb_missions(rdb_path):
    """{path: GUID} for Missions/*.conf from a resourceDatabase.rdb."""
    try:
        with open(rdb_path, "rb") as f:
            d = f.read()
    except OSError:
        return {}
    out = {}
    for m in re.finditer(rb"([\x20-\x7e]*?Missions/[\x20-\x7e]+?\.conf)\x00\x06\x00\x00\x00\x00\x00(.{8})", d, re.S):
        path = m.group(1).decode()
        # the path is stored whole; the regex can start early inside a previous record
        k = path.find("Missions/")
        path = path[k:] if k >= 0 else path
        out[path] = m.group(2)[::-1].hex().upper()
    return out


def _pak_index(pak):
    """{path: (offset, csize, usize)} from a .pak's FILE chunk."""
    out = {}
    with open(pak, "rb") as f:
        f.seek(0, 2)
        n = f.tell()
        i = 12
        fc = None
        while i + 8 <= n:
            f.seek(i)
            h = f.read(8)
            cc, sz = h[:4], struct.unpack(">I", h[4:])[0]
            if cc == b"FILE":
                f.seek(i + 8)
                fc = f.read(sz)
                break
            i += 8 + sz
    if fc is None:
        return out

    def walk(p, path):
        typ, ln = fc[p], fc[p + 1]
        name = fc[p + 2:p + 2 + ln].decode("utf-8", "replace")
        p += 2 + ln
        if typ == 0:
            c = struct.unpack("<I", fc[p:p + 4])[0]
            p += 4
            for _ in range(c):
                p = walk(p, path + name + "/")
            return p
        o, cs, us = struct.unpack("<III", fc[p:p + 12])
        out[(path + name).lstrip("/")] = (o, cs, us)
        return p + 24
    walk(0, "")
    return out


def _read_header(pak, entry):
    o, cs, us = entry
    with open(pak, "rb") as f:
        f.seek(o)
        b = f.read(cs)
    if cs != us:
        try:
            b = zlib.decompress(b)
        except Exception:
            return None
    t = b.decode("utf-8", "replace")
    if "MissionHeader" not in t.split("{", 1)[0]:
        return None
    def field(k):
        m = re.search(r'\b%s\s+"([^"]*)"' % k, t)
        return m.group(1) if m else ""
    pc = re.search(r"\bm_iPlayerCount\s+(\d+)", t)
    return {"name": field("m_sName"), "mode": field("m_sGameMode"), "world": field("World"),
            "players": int(pc.group(1)) if pc else 0}


def _nice(path, name):
    if name and not name.startswith("#"):
        return name
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"^\d+_", "", base)
    return base.replace("_", " ")


def _scenarios_in_packed(folder):
    rdb = os.path.join(folder, "resourceDatabase.rdb")
    guids = _rdb_missions(rdb)
    if not guids:
        return []
    paks = sorted(p for p in _listdir(folder) if p.endswith(".pak"))
    found = {}
    for p in paks:
        pp = os.path.join(folder, p)
        try:
            idx = _pak_index(pp)
        except Exception as e:
            log("[play] pak index failed", pp, e)
            continue
        for path, guid in guids.items():
            if path in idx and path not in found:
                h = _read_header(pp, idx[path])
                if h:
                    found[path] = dict(h, path=path, guid=guid)
    return list(found.values())


def _scenarios_in_source(folder):
    out = []
    md = os.path.join(folder, "Missions")
    for n in _listdir(md):
        if not n.endswith(".conf"):
            continue
        p = os.path.join(md, n)
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                t = f.read()
            with open(p + ".meta", "r", encoding="utf-8-sig", errors="replace") as f:
                mt = f.read()
        except OSError:
            continue
        if "MissionHeader" not in t.split("{", 1)[0]:
            continue
        g = re.search(r"\{([0-9A-Fa-f]{16})\}", mt)
        if not g:
            continue
        nm = re.search(r'\bm_sName\s+"([^"]*)"', t)
        gm = re.search(r'\bm_sGameMode\s+"([^"]*)"', t)
        pc = re.search(r"\bm_iPlayerCount\s+(\d+)", t)
        out.append({"path": "Missions/" + n, "guid": g.group(1).upper(), "name": nm.group(1) if nm else "",
                    "mode": gm.group(1) if gm else "", "players": int(pc.group(1)) if pc else 0})
    return out


def _cached_scan(key_path, stamp, fn):
    with _cache_lock:
        c = _load_cache()
        hit = c.get(key_path)
        if hit and hit.get("stamp") == stamp:
            return hit["items"]
    items = fn()
    with _cache_lock:
        c = _load_cache()
        c[key_path] = {"stamp": stamp, "items": items}
        _save_cache(c)
    return items


def _stamp(folder, pattern=".pak"):
    parts = []
    for n in sorted(_listdir(folder)):
        if n.endswith(pattern) or n == "resourceDatabase.rdb":
            try:
                st = os.stat(os.path.join(folder, n))
                parts.append("%s:%d:%d" % (n, st.st_size, int(st.st_mtime)))
            except OSError:
                pass
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def scenarios_for(mod):
    folder = mod.get("build_path") if mod.get("build") == "packed" and mod.get("build_path") else mod["path"]
    if os.path.exists(os.path.join(folder, "data.pak")):
        items = _cached_scan(folder, _stamp(folder), lambda: _scenarios_in_packed(folder))
    else:
        items = _scenarios_in_source(folder)
    return [dict(s, label=_nice(s["path"], s.get("name")), rid="{%s}%s" % (s["guid"], s["path"]),
                 owner=mod["guid"], owner_name=mod["name"]) for s in items]


def vanilla_scenarios():
    g = game_dir()
    if not g:
        return []
    folder = os.path.join(g, "addons", "data")
    items = _cached_scan(folder, _stamp(folder), lambda: _scenarios_in_packed(folder))
    return [dict(s, label=_nice(s["path"], s.get("name")), rid="{%s}%s" % (s["guid"], s["path"]),
                 owner="", owner_name="Arma Reforger") for s in items]


# ----------------------------------------------------------------------------
# settings (last selection, presets)
# ----------------------------------------------------------------------------
def settings_get():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("mods", [])
    s.setdefault("scenario", "")
    s.setdefault("load_save", True)
    s.setdefault("presets", {})
    s.setdefault("extra_args", "")
    return s


def settings_put(s):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


# ----------------------------------------------------------------------------
# load order and launch
# ----------------------------------------------------------------------------
def resolve(selected, mods):
    """Selected GUIDs plus everything they depend on, dependencies first,
    otherwise in the order picked. Returns (order, added, missing)."""
    order, added, missing, seen = [], [], [], set()

    def visit(g, chain):
        if g in seen or g in BASE_GUIDS:
            return
        if g in chain:
            return
        m = mods.get(g)
        if not m:
            if g not in missing:
                missing.append(g)
            return
        for d in m.get("deps") or []:
            visit(d, chain | {g})
        if g not in seen:
            seen.add(g)
            order.append(g)
            if g not in selected:
                added.append(g)
    for g in selected:
        visit(g.upper(), set())
    return order, added, missing


def build_command(order, mods, scenario_rid, load_save, extra=""):
    g = game_dir()
    exe = os.path.join(g, "ArmaReforgerSteam.exe") if g else "ArmaReforgerSteam.exe"
    dirs = [local_mods_dir(), addons_dir()]
    args = [exe, "-addonsDir", ",".join(dirs), "-addons", ",".join(order)]
    if scenario_rid:
        args += ["-server", scenario_rid]
        if load_save:
            args.append("-loadSessionSave")
    if extra:
        args += extra.split()
    return args


def _mirror_loose(mod):
    """Copy a Workbench project into localmods so the game can load it."""
    dst = os.path.join(local_mods_dir(), mod["folder"])
    os.makedirs(local_mods_dir(), exist_ok=True)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(mod["path"], dst, ignore=shutil.ignore_patterns(".git", ".vs", "*.pak", "*_manifest.json", "ServerData.json", "meta"))
    return dst


def prepare_local(order, mods):
    """Loose local projects get copied fresh; packed builds are used as they are.
    Also clears any other localmods folder with the same GUID, so the game
    never sees two copies of one mod."""
    done = []
    for guid in order:
        m = mods.get(guid)
        if not m or m["kind"] != "local" or m.get("build") != "loose":
            continue
        _drop_other_copies(guid, keep=os.path.join(local_mods_dir(), m["folder"]))
        _mirror_loose(m)
        done.append(m["name"])
    return done


def _drop_other_copies(guid, keep):
    ld = local_mods_dir()
    for n in _listdir(ld):
        f = os.path.join(ld, n)
        if os.path.normcase(f) == os.path.normcase(keep):
            continue
        g = read_gproj(f)
        if g and g["guid"] == guid:
            shutil.rmtree(f, ignore_errors=True)
            log("[play] removed duplicate copy", f)



# ----------------------------------------------------------------------------
# Steam has to be running and signed in, or the game dies at start with
# "SteamAPI_Init failed. Is Steam running?" when started directly from its exe.
# ----------------------------------------------------------------------------
def _steam_reg(name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam" + ("\\ActiveProcess" if name in ("ActiveUser", "pid") else "")) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def steam_exe():
    p = _steam_reg("SteamExe")
    if p and os.path.isfile(p):
        return os.path.normpath(p)
    for c in (r"C:\Program Files (x86)\Steam\steam.exe", r"C:\Program Files\Steam\steam.exe"):
        if os.path.isfile(c):
            return c
    return ""


def _steam_process_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True, text=True,
                             creationflags=0x08000000).stdout
        return "steam.exe" in out.lower()
    except Exception:
        return True   # can't tell: don't block the launch


def steam_ready():
    """Steam is running and someone is signed in to it."""
    if os.name != "nt":
        return True
    return _steam_process_running() and bool(_steam_reg("ActiveUser"))


def ensure_steam(timeout=90):
    """Start Steam (minimised) if it isn't running and signed in, and wait for it. Returns an error
    string, or "" when Steam is ready."""
    if steam_ready():
        return ""
    exe = steam_exe()
    if not exe:
        return "Steam isn't running and steam.exe wasn't found. Start Steam, then launch again."
    log("[play] Steam not ready, starting it:", exe)
    try:
        subprocess.Popen([exe, "-silent"], creationflags=0x00000008)
    except Exception as e:
        return "Steam isn't running and couldn't be started (%s). Start Steam, then launch again." % e
    t = time.time()
    while time.time() - t < timeout:
        time.sleep(2)
        if steam_ready():
            time.sleep(5)   # signed in; give it a moment to finish starting its services
            return ""
    return "Steam didn't finish signing in. Sign in to Steam, then launch again."


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


STEAM_APP_ID = "1874880"
START = {"at": 0, "error": ""}   # the last game start, and why it died if it did


def start_game(args, cwd):
    """Start the game exe. Steam refuses a game running at a different privilege level than itself
    ("SteamAPI_Init failed"), so a launcher that is running as administrator starts the game
    un-elevated through Explorer. Then watches the game log for that failure."""
    START.update(at=time.time(), error="")
    # What Steam itself sets when it starts the game. Without it the game finds its app id only
    # through steam_appid.txt in the game folder, and fails with "SteamAPI_Init failed" when that
    # file is missing.
    env = dict(os.environ, SteamAppId=STEAM_APP_ID, SteamGameId=STEAM_APP_ID, SteamOverlayGameId=STEAM_APP_ID)
    if os.name == "nt" and is_admin():
        bat = os.path.join(HERE, "_start_game.bat")
        with open(bat, "w", newline="\r\n") as f:
            f.write('@echo off\nset SteamAppId=%s\nset SteamGameId=%s\ncd /d "%s"\nstart "" %s\n'
                    % (STEAM_APP_ID, STEAM_APP_ID, cwd, subprocess.list2cmdline(args)))
        log("[play] launcher is running as administrator: starting the game un-elevated through Explorer")
        subprocess.Popen(["explorer.exe", bat])
    else:
        subprocess.Popen(args, cwd=cwd, env=env, creationflags=0x00000008 if os.name == "nt" else 0)   # DETACHED_PROCESS
    threading.Thread(target=_watch_start, args=(START["at"],), daemon=True).start()


def _watch_start(t0):
    logs = os.path.join(os.path.dirname(addons_dir()), "logs")
    path = None
    while time.time() - t0 < 60 and START["at"] == t0:
        time.sleep(1)
        try:
            if not path:
                ds = [os.path.join(logs, d) for d in os.listdir(logs) if d.startswith("logs_")]
                ds = [d for d in ds if os.path.getctime(d) >= t0 - 5 and os.path.isfile(os.path.join(d, "console.log"))]
                if ds:
                    path = os.path.join(max(ds, key=os.path.getctime), "console.log")
                continue
            with open(path, "rb") as f:
                txt = f.read().decode("utf-8", "replace")
            if "SteamAPI_Init failed" in txt:
                running, signed_in = _steam_process_running(), bool(_steam_reg("ActiveUser"))
                if not running:
                    why = "Steam isn't running. Open Steam, sign in, and launch again."
                elif not signed_in:
                    why = "Steam is open but not signed in. Sign in, then launch again."
                else:
                    why = ("Steam is open but refused the game. Usually Steam and this launcher are running at different "
                           "levels: close both and start them normally (not 'Run as administrator'), or both as administrator.")
                START["error"] = "The game couldn't connect to Steam. " + why
                log("[play] game start failed: SteamAPI_Init failed (steam running=%s, signed in=%s, launcher admin=%s)"
                    % (running, signed_in, is_admin()))
                return
            if "Game successfully created" in txt or "Unable to initialize the game" in txt:
                if "Unable to initialize the game" in txt:
                    START["error"] = "The game closed while starting. Check its console.log in Documents\\My Games\\ArmaReforger\\logs."
                return
        except Exception:
            pass

def launch(sel, scenario_rid, load_save, extra, game_running):
    if game_running():
        return {"ok": False, "error": "Arma Reforger is already running. Close it first."}
    g = game_dir()
    if not g:
        return {"ok": False, "error": "Arma Reforger install not found. Set game_dir in config.json."}
    mods = scan_mods()
    order, added, missing = resolve(sel, mods)
    if missing:
        return {"ok": False, "error": "Missing dependencies (not downloaded): " + ", ".join(missing), "missing": missing}
    if not scenario_rid:
        return {"ok": False, "error": "Pick a scenario."}
    try:
        copied = prepare_local(order, mods)
    except Exception as e:
        return {"ok": False, "error": "Could not copy local mods: %s" % e}
    args = build_command(order, mods, scenario_rid, load_save, extra)
    err = ensure_steam()
    if err:
        return {"ok": False, "error": err}
    log("[play] launching:", subprocess.list2cmdline(args))
    try:
        start_game(args, g)
    except Exception as e:
        return {"ok": False, "error": "Could not start the game: %s" % e}
    return {"ok": True, "order": order, "added": added, "copied": copied,
            "command": subprocess.list2cmdline(args)}


# ----------------------------------------------------------------------------
# building a local project into a packed mod (Workbench -packAddon)
# ----------------------------------------------------------------------------
BUILD = {"running": False, "guid": "", "name": "", "msg": "", "ok": None, "at": 0}


def build_start(guid):
    if BUILD["running"]:
        return {"ok": False, "error": "A build is already running."}
    mods = scan_mods()
    m = mods.get(guid.upper())
    if not m or m["kind"] != "local":
        return {"ok": False, "error": "Not a local project."}
    wb = workbench_exe()
    if not wb:
        return {"ok": False, "error": "Arma Reforger Tools (Workbench) not found. Set workbench_exe in config.json."}
    BUILD.update(running=True, guid=m["guid"], name=m["name"], msg="Packing with Workbench...", ok=None, at=time.time())
    threading.Thread(target=_build, args=(m, wb), daemon=True).start()
    return {"ok": True}


def _build(m, wb):
    try:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_build", m["folder"])
        if os.path.isdir(out):
            shutil.rmtree(out)
        os.makedirs(out)
        src = m["path"]
        gproj = os.path.join(src, m["gproj"])
        g = game_dir()
        dirs = [os.path.join(g, "addons"), os.path.join(os.path.dirname(wb), "addons"), workbench_addons_dir(), addons_dir()]  # addons_dir: Workshop deps (RHS, FORTEX...)
        args = [wb, "-addonsDir", ",".join(d for d in dirs if os.path.isdir(d)), "-gproj", gproj,
                "-wbModule=ResourceManager", "-packAddon", "-packAddonDir", out, "-exitAfterInit"]
        log("[play] build:", subprocess.list2cmdline(args))
        p = subprocess.Popen(args, cwd=os.path.dirname(wb), creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            p.wait(timeout=900)
        except subprocess.TimeoutExpired:
            p.kill()
            raise RuntimeError("Workbench did not finish within 15 minutes")
        pak = os.path.join(out, "data.pak")
        if not os.path.exists(pak):
            raise RuntimeError("Workbench produced no data.pak (check the Workbench log)")
        dst = os.path.join(local_mods_dir(), m["folder"])
        _drop_other_copies(m["guid"], keep=dst)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        os.makedirs(dst)
        for n in ("data.pak", "resourceDatabase.rdb", m["gproj"]):
            s = os.path.join(out, n)
            if not os.path.exists(s):
                s = os.path.join(src, n)
            if os.path.exists(s):
                shutil.copyfile(s, os.path.join(dst, n))
        BUILD.update(msg="Built %s (%d KB)" % (m["name"], os.path.getsize(pak) // 1024), ok=True)
        log("[play] build done ->", dst)
    except Exception as e:
        BUILD.update(msg="Build failed: %s" % e, ok=False)
        log("[play] build failed", e)
    finally:
        BUILD.update(running=False, at=time.time())
        invalidate_scan()


def unbuild(guid):
    """Remove a packed local build so the project loads loose (from source) again."""
    mods = scan_mods()
    m = mods.get(guid.upper())
    if not m or m["kind"] != "local" or m.get("build") != "packed":
        return {"ok": False, "error": "No packed build for that mod."}
    shutil.rmtree(m["build_path"], ignore_errors=True)
    invalidate_scan()
    return {"ok": True}


# ----------------------------------------------------------------------------
# images shipped with mods (thumbnail.png, preview*.jpg, scenario*.jpg)
# ----------------------------------------------------------------------------
IMAGES = {}   # "thumb:<GUID>" / "scen:<GUID>" -> file path


def _find_image(folders, want):
    files = []
    for f in folders:
        for n in _listdir(f):
            if n.lower().endswith((".png", ".jpg", ".jpeg")) and os.path.isfile(os.path.join(f, n)):
                files.append((n.lower(), os.path.join(f, n)))
    if not files:
        return ""

    def width(n):
        m = re.search(r"_(\d+)x\d+", n)
        return int(m.group(1)) if m else 0
    if want == "scen":
        sc = sorted((x for x in files if x[0].startswith("scenario")), key=lambda x: width(x[0]))
        if sc:
            return sc[-1][1]
    # small previews first (fast to load), then the thumbnail, then anything
    pv = sorted((x for x in files if x[0].startswith("preview")), key=lambda x: width(x[0]))
    small = [x for x in pv if 0 < width(x[0]) <= 640]
    if small:
        return small[-1][1]
    for n, p in files:
        if n == "thumbnail.png":
            return p
    if pv:
        return pv[0][1]
    return sorted(files)[0][1]


def _image_folders(m):
    out = [m["path"]]
    if m.get("build_path"):
        out.append(m["build_path"])
    # a Workbench copy of a Workshop download often carries the images the game dropped
    wb = os.path.join(workbench_addons_dir(), os.path.basename(m["path"].rstrip("\\/")))
    if wb not in out and os.path.isdir(wb):
        out.append(wb)
    return out


def image_path(key):
    return IMAGES.get(key, "")


# ----------------------------------------------------------------------------
# api helpers
# ----------------------------------------------------------------------------
def state():
    mods = scan_mods()
    s = settings_get()
    lst = []
    for g, m in mods.items():
        row = {k: m.get(k) for k in ("guid", "name", "kind", "build", "stale", "built_at", "deps", "version", "folder")}
        img = _find_image(_image_folders(m), "thumb")
        if img:
            IMAGES["thumb:" + g] = img
            row["img"] = "/api/play/img/thumb/" + g
        lst.append(row)
    lst.sort(key=lambda x: (x["kind"] != "local", (x["name"] or "").lower()))
    return {"mods": lst, "settings": s, "game_dir": game_dir(), "workbench": workbench_exe(),
            "local_dir": local_mods_dir(), "build": BUILD}


def scenarios(sel):
    mods = scan_mods()
    order, added, missing = resolve(sel, mods)
    out = []
    for g in order:
        img = _find_image(_image_folders(mods[g]), "scen")
        if img:
            IMAGES["scen:" + g] = img
        try:
            out += [dict(x, img=("/api/play/img/scen/" + g) if img else "") for x in scenarios_for(mods[g])]
        except Exception as e:
            log("[play] scenario scan failed for", g, e)
    try:
        van = vanilla_scenarios()
    except Exception as e:
        log("[play] vanilla scan failed", e)
        van = []
    return {"order": order, "added": added, "missing": missing, "scenarios": out, "vanilla": van,
            "command": subprocess.list2cmdline(build_command(order, mods, settings_get().get("scenario") or "{...}", settings_get().get("load_save")))}
