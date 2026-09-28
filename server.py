#!/usr/bin/env python3
"""
Reforger Mod Browser: a local, Steam-style browser for the Arma Reforger
workshop.

  python server.py            start on http://127.0.0.1:8765 and open it
  python server.py --sync     full sync of the workshop into mods.db, then exit
  python server.py --port N

Standard library only. Data comes from the public workshop site's JSON feed
(the same data its own pages render). The browser can't subscribe for you;
it writes the mods you tick to a list file in your Reforger profile, and the
companion in-game mod downloads them with the game's own workshop action.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "server.log")
_log_lock = threading.Lock()


def log(*parts):
    """print() goes nowhere under pythonw, so anything worth keeping goes here too."""
    line = "%s %s" % (time.strftime("%H:%M:%S"), " ".join(str(p) for p in parts))
    try:
        print(line)
    except Exception:
        pass
    try:
        with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
SITE = "https://reforger.armaplatform.com"
UA = "Mozilla/5.0 (ReforgerModBrowser; local tool)"
PAGE_SIZE = 16          # what the site returns per page
REQ_GAP = 0.08          # seconds between requests across all workers
WORKERS = 6             # parallel page fetches during a sync
DB_PATH = os.path.join(HERE, "mods.db")
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    # where the game keeps downloaded mods: folder names end in _<ID>
    "addons_dir": os.path.expanduser(r"~\Documents\My Games\ArmaReforger\addons"),
    # more folders the game loads mods from (what you pass to -addonsDir); the
    # Workbench's own folder holds the workshop mods it downloaded too
    "addons_dirs_extra": [],
    # the folder the game is pointed at with -addonsDir. Only the companion and
    # other local mods live here; pointing the game at the Workbench addons
    # folder breaks the workshop's addon check (it never finishes), so the dev
    # projects are mirrored into this clean folder before every launch
    "local_mods_dir": os.path.expanduser(r"~\Documents\My Games\ArmaReforger\localmods"),
    "local_mods_src": [
        os.path.expanduser(r"~\Documents\My Games\ArmaReforgerWorkbench\addons\BetterWorkshop"),
        os.path.expanduser(r"~\Documents\My Games\ArmaReforgerWorkbench\addons\SL_Gunplay"),
    ],
    # $profile:BetterWorkshop of the game; the in-game companion reads queue.txt
    # from here and writes status.txt / applied.txt back
    "queue_dir": os.path.expanduser(r"~\Documents\My Games\ArmaReforger\profile\BetterWorkshop"),
    # the game launched from Workbench uses the Workbench profile instead; the
    # playlist is written to every folder listed here that exists
    "queue_dirs_extra": [os.path.expanduser(r"~\Documents\My Games\ArmaReforgerWorkbench\profile\BetterWorkshop")],
    "port": 8765,
    # start Arma Reforger (through Steam) when the browser opens, if it is not running
    "launch_game_on_open": True,
    "steam_app_id": 1874880,
    # re-sync recently updated mods this often while running (minutes); 0 = never
    "auto_sync_minutes": 30,
}


# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
        except Exception as e:
            print(f"[config] could not read config.json: {e}")
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    cfg.update(user)
    # older config.json files named the file, not the folder
    if user.get("queue_file") and not user.get("queue_dir"):
        cfg["queue_dir"] = os.path.dirname(user["queue_file"])
    cfg.pop("queue_file", None)
    return cfg


def queue_dirs():
    """Folders the queue is written to: the game profile always, the extra
    ones only when their parent (the profile) exists."""
    out = []
    if CONFIG.get("queue_dir"):
        out.append(CONFIG["queue_dir"])
    for d in CONFIG.get("queue_dirs_extra") or []:
        if d and d not in out and os.path.isdir(os.path.dirname(d)):
            out.append(d)
    return out


def status_dir():
    """Where the companion last reported: the folder with the newest status.txt."""
    best, best_t = CONFIG.get("queue_dir") or "", -1
    for d in queue_dirs():
        try:
            t = os.path.getmtime(os.path.join(d, "status.txt"))
        except OSError:
            continue
        if t > best_t:
            best, best_t = d, t
    return best


CONFIG = load_config()

import play   # Play tab: direct launch with local mods
play.init(CONFIG, log)
import party  # play together over Radmin
party.init(CONFIG, log)
import wsdl   # Workshop downloads without the game
wsdl.init(CONFIG, log)
import update  # app updates over Radmin
update.init(CONFIG, log)


# ----------------------------------------------------------------------------
# database
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS mods (
    id TEXT PRIMARY KEY,
    name TEXT,
    author TEXT,
    author_id TEXT,
    summary TEXT,
    type TEXT,
    rating REAL,          -- 0..1
    votes INTEGER,
    subscribers INTEGER,
    version TEXT,
    size INTEGER,         -- bytes
    thumb TEXT,
    created_at TEXT,      -- ISO
    updated_at TEXT,
    tags TEXT,            -- comma separated, upper case
    game_version TEXT,
    row_json TEXT,
    detail_json TEXT,     -- full mod page data, fetched on demand
    detail_at REAL,
    seen_at REAL,
    downloads INTEGER,    -- from the mod page; the site's subscriber count is unreliable
    downloads_at REAL
);
CREATE INDEX IF NOT EXISTS mods_updated ON mods(updated_at);
CREATE INDEX IF NOT EXISTS mods_created ON mods(created_at);
CREATE INDEX IF NOT EXISTS mods_subs ON mods(subscribers);
CREATE INDEX IF NOT EXISTS mods_votes ON mods(votes);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS list (
    id TEXT PRIMARY KEY,
    added_at REAL
);
-- one row per mod per day: what "trending" is measured from
CREATE TABLE IF NOT EXISTS stats (
    id TEXT, day TEXT, subs INTEGER, votes INTEGER, rating REAL, dl INTEGER,
    PRIMARY KEY (id, day)
);
CREATE TABLE IF NOT EXISTS favorites (id TEXT PRIMARY KEY, added_at REAL);
CREATE TABLE IF NOT EXISTS deps (id TEXT, dep TEXT, PRIMARY KEY (id, dep));
CREATE INDEX IF NOT EXISTS deps_dep ON deps(dep);
CREATE TABLE IF NOT EXISTS playlists (pid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, created_at REAL);
CREATE TABLE IF NOT EXISTS playlist_mods (pid INTEGER, id TEXT, added_at REAL, PRIMARY KEY (pid, id));
-- installed mods the user asked to delete from disk; the in-game companion does it
CREATE TABLE IF NOT EXISTS removals (id TEXT PRIMARY KEY, name TEXT, queued_at REAL);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # log10 is only built in when sqlite was compiled with math functions
    try:
        conn.execute("SELECT log10(10)")
    except sqlite3.OperationalError:
        conn.create_function("log10", 1, lambda x: math.log10(x) if x and x > 0 else 0.0)
    return conn


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(mods)").fetchall()}
        if "downloads" not in cols:
            conn.execute("ALTER TABLE mods ADD COLUMN downloads INTEGER")
            conn.execute("ALTER TABLE mods ADD COLUMN downloads_at REAL")
        scols = {r[1] for r in conn.execute("PRAGMA table_info(stats)").fetchall()}
        if "dl" not in scols:
            conn.execute("ALTER TABLE stats ADD COLUMN dl INTEGER")
        conn.execute("CREATE TABLE IF NOT EXISTS details (id TEXT PRIMARY KEY, data BLOB, at REAL)")
        if conn.execute("SELECT COUNT(*) FROM deps").fetchone()[0] == 0:
            for r in conn.execute("SELECT id, data FROM details").fetchall():
                try:
                    store_deps(conn, r["id"], detail_unpack(r["data"]))
                except Exception:
                    pass
        if conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 0:
            conn.execute("INSERT INTO playlists(name, created_at) VALUES('Default', ?)", (time.time(),))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('active_playlist', (SELECT pid FROM playlists LIMIT 1))")


def detail_pack(pp):
    return zlib.compress(json.dumps(pp, separators=(",", ":")).encode("utf-8"), 6)


def detail_unpack(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return json.loads(zlib.decompress(v).decode("utf-8"))
    return json.loads(v)


def slim_db():
    """One-time: the full mod pages used to sit inside the mods table (about 500 MB), so every
    list query that sorted by downloads dragged them off disk. Move them, compressed, into their
    own table and shrink the file. Runs before the server takes requests."""
    try:
        with db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM mods WHERE detail_json IS NOT NULL").fetchone()[0]
        if not n:
            return
        size0 = os.path.getsize(DB_PATH)
        log("[db] moving %d stored mod pages out of the mods table (one time, about a minute)" % n)
        t = time.time()
        with db() as conn:
            rows = conn.execute("SELECT id, detail_json, detail_at FROM mods WHERE detail_json IS NOT NULL").fetchall()
            batch = []
            for r in rows:
                v = r["detail_json"]
                raw = v.encode("utf-8") if isinstance(v, str) else bytes(v)
                batch.append((r["id"], zlib.compress(raw, 6), r["detail_at"] or 0))
            conn.executemany("INSERT OR REPLACE INTO details(id, data, at) VALUES(?,?,?)", batch)
            conn.execute("UPDATE mods SET detail_json = NULL WHERE detail_json IS NOT NULL")
        v = sqlite3.connect(DB_PATH, timeout=120, isolation_level=None)
        v.execute("VACUUM")
        v.close()
        log("[db] done in %ds: %d MB -> %d MB" % (time.time() - t, size0 // 1048576, os.path.getsize(DB_PATH) // 1048576))
    except Exception as e:
        log("[db] slimming failed, keeping the database as it was:", e)


def db_health():
    """Quick integrity check at start; the result shows in the app log."""
    try:
        v = sqlite3.connect(DB_PATH, timeout=60)
        r = v.execute("PRAGMA quick_check(5)").fetchall()
        v.close()
        ok = len(r) == 1 and r[0][0] == "ok"
        log("[db] integrity:", "ok" if ok else "PROBLEM " + "; ".join(x[0] for x in r)[:500])
        return ok
    except Exception as e:
        log("[db] integrity check failed:", e)
        return False


def meta_get(key, default=None):
    with db() as conn:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def meta_set(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))


# ----------------------------------------------------------------------------
# workshop site access
# ----------------------------------------------------------------------------
class Site:
    def __init__(self):
        self.build_id = None
        self.lock = threading.Lock()
        self.last_req = 0.0

    def _get(self, url, retries=4):
        for attempt in range(retries):
            with self.lock:
                gap = REQ_GAP - (time.time() - self.last_req)
                if gap > 0:
                    time.sleep(gap)
                self.last_req = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except Exception:
                time.sleep(2 * (attempt + 1))
        return None

    def refresh_build_id(self):
        html = self._get(SITE + "/workshop")
        if not html:
            raise RuntimeError("could not load the workshop page")
        m = re.search(rb'"buildId":"([^"]+)"', html)
        if not m:
            raise RuntimeError("no buildId on the workshop page (site changed?)")
        self.build_id = m.group(1).decode()
        return self.build_id

    def _data(self, path, query=""):
        """Next.js data route. Refreshes the build id once if it went stale."""
        if not self.build_id:
            self.refresh_build_id()
        for _ in range(2):
            url = f"{SITE}/_next/data/{self.build_id}{path}.json{query}"
            raw = self._get(url)
            if raw is None:
                self.refresh_build_id()
                continue
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                self.refresh_build_id()
        return None

    def list_page(self, sort="newest", page=1, search=""):
        q = f"?sort={sort}&page={page}"
        if search:
            q += "&search=" + urllib.parse.quote(search)
        d = self._data("/workshop", q)
        if not d:
            return None, 0
        pp = d.get("pageProps", {})
        assets = pp.get("assets", {})
        return assets.get("rows", []), assets.get("count", 0)

    def detail(self, mod_id):
        d = self._data(f"/workshop/{mod_id}", f"?pathId={mod_id}")
        if not d:
            return None
        return d.get("pageProps")


SITE_API = Site()


def row_to_record(r, now):
    tags = ",".join(sorted({(t.get("name") or "").upper() for t in (r.get("tags") or []) if t.get("name")}))
    thumb = ""
    prev = r.get("previews") or []
    if prev:
        p = prev[0]
        thumb = p.get("url") or ""
        # the site has small jpeg thumbnails; use the smallest that is still readable
        th = (p.get("thumbnails") or {}).get("image/jpeg") or []
        if th:
            th = sorted(th, key=lambda t: t.get("width", 0))
            for t in th:
                if t.get("width", 0) >= 320:
                    thumb = t.get("url", thumb)
                    break
    author = r.get("author") or {}
    dep = r.get("dependencyTree") or {}
    return (
        r.get("id"),
        r.get("name") or "",
        author.get("username") or "",
        author.get("id") or "",
        r.get("summary") or "",
        r.get("type") or "",
        float(r.get("averageRating") or 0),
        int(r.get("ratingCount") or 0),
        int(r.get("subscriberCount") or 0),
        r.get("currentVersionNumber") or "",
        int(r.get("currentVersionSize") or 0),
        thumb,
        r.get("createdAt") or "",
        r.get("updatedAt") or "",
        tags,
        dep.get("gameVersion") or "",
        json.dumps(r),
        now,
    )


UPSERT = """
INSERT INTO mods (id,name,author,author_id,summary,type,rating,votes,subscribers,version,size,thumb,
                  created_at,updated_at,tags,game_version,row_json,seen_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
  name=excluded.name, author=excluded.author, author_id=excluded.author_id, summary=excluded.summary,
  type=excluded.type, rating=excluded.rating, votes=excluded.votes, subscribers=excluded.subscribers,
  version=excluded.version, size=excluded.size, thumb=excluded.thumb, created_at=excluded.created_at,
  updated_at=excluded.updated_at, tags=excluded.tags, game_version=excluded.game_version,
  row_json=excluded.row_json, seen_at=excluded.seen_at
"""


# ----------------------------------------------------------------------------
# sync
# ----------------------------------------------------------------------------
class Sync:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.status = {"phase": "idle", "page": 0, "pages": 0, "done": 0, "total": 0, "error": "", "quiet": False, "first": False}
        self.stop_flag = False

    def _set(self, **kw):
        self.status.update(kw)

    def full(self):
        """Every mod, newest first. 49k mods / 16 per page = ~3100 requests."""
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, args=("full",), daemon=True).start()
        return True

    def incremental(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, args=("incremental",), daemon=True).start()
        return True

    def _run(self, mode):
        try:
            self.stop_flag = False
            SITE_API.refresh_build_id()
            if mode == "full":
                # the good stuff first so the page is useful within a minute,
                # then every mod there is
                self._set(quiet=False, first=not meta_get("full_sync_at"))
                self._walk("popular", stop_when_known=False, max_pages=150)
                self._walk("newest", stop_when_known=False)
                meta_set("full_sync_at", time.time())
            else:
                self._set(quiet=True, first=False)
                self._walk("updated", stop_when_known=True)
            meta_set("last_sync_at", time.time())
            self._set(phase="idle", error="")
        except Exception as e:
            self._set(phase="error", error=str(e))
            print(f"[sync] {e}")
        finally:
            self.running = False

    def _walk(self, sort, stop_when_known, max_pages=0):
        last_sync = float(meta_get("last_sync_at", 0) or 0)
        # margin: the site's clock and ours, plus mods updated during the last walk
        cutoff = last_sync - 600 if stop_when_known else 0
        now = time.time()
        rows, count = SITE_API.list_page(sort, 1)
        if rows is None:
            raise RuntimeError("workshop feed not reachable")
        pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        if max_pages:
            pages = min(pages, max_pages)
        self._set(phase=sort, page=1, pages=pages, done=0, total=count)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lock = threading.Lock()
        state = {"seen": 0, "next": 2, "stop": False, "done_pages": 1}

        def store(rs):
            recs = [row_to_record(r, now) for r in rs if r.get("id")]
            with db() as conn:
                conn.executemany(UPSERT, recs)
                conn.executemany("INSERT OR REPLACE INTO stats(id,day,subs,votes,rating) VALUES(?,?,?,?,?)",
                                 [(r[0], day, r[8], r[7], r[6]) for r in recs])
            return len(recs)

        n0 = store(rows)
        with lock:
            state["seen"] += n0
        if stop_when_known and rows and min(_iso_to_ts(r.get("updatedAt")) for r in rows) < cutoff:
            state["stop"] = True

        def worker():
            while True:
                with lock:
                    if state["stop"] or self.stop_flag or state["next"] > pages:
                        return
                    page = state["next"]
                    state["next"] += 1
                rs, _ = SITE_API.list_page(sort, page)
                if rs is None:
                    rs = []
                n = store(rs)
                with lock:
                    state["seen"] += n
                    state["done_pages"] += 1
                    self._set(page=state["done_pages"], done=state["seen"])
                    if stop_when_known and rs and min(_iso_to_ts(r.get("updatedAt")) for r in rs) < cutoff:
                        state["stop"] = True

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        print(f"[sync] {sort}: {state['seen']} mods over {state['done_pages']} pages")


# ----------------------------------------------------------------------------
# enrichment: downloads from each mod's page, most-voted first
# ----------------------------------------------------------------------------
class Enrich:
    def __init__(self):
        self.running = False
        self.done = 0
        self.todo = 0

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            while True:
                if SYNC.running:
                    time.sleep(5)
                    continue
                stale = time.time() - 7 * 86400
                with db() as conn:
                    self.todo = conn.execute("SELECT COUNT(*) FROM mods WHERE downloads IS NULL OR downloads_at < ?", (stale,)).fetchone()[0]
                    rows = conn.execute(
                        "SELECT id FROM mods WHERE downloads IS NULL OR downloads_at < ? ORDER BY votes DESC, subscribers DESC LIMIT 50",
                        (stale,)).fetchall()
                if not rows:
                    time.sleep(600)
                    continue
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                for r in rows:
                    if SYNC.running:
                        break
                    pp = SITE_API.detail(r["id"])
                    dl = None
                    if pp:
                        dl = (pp.get("getAssetDownloadTotal") or {}).get("total")
                    with db() as conn:
                        if pp:
                            conn.execute("UPDATE mods SET downloads=?, downloads_at=? WHERE id=?",
                                         (int(dl or 0), time.time(), r["id"]))
                            conn.execute("INSERT OR REPLACE INTO details(id, data, at) VALUES(?,?,?)",
                                         (r["id"], detail_pack(pp), time.time()))
                            store_deps(conn, r["id"], pp)
                        else:
                            conn.execute("UPDATE mods SET downloads=COALESCE(downloads, 0), downloads_at=? WHERE id=?", (time.time(), r["id"]))
                        conn.execute("UPDATE stats SET dl=? WHERE id=? AND day=?", (int(dl or 0), r["id"], day))
                    self.done += 1
        except Exception as e:
            print(f"[enrich] {e}")
        finally:
            self.running = False


ENRICH = Enrich()


def store_deps(conn, mod_id, pp):
    a = (pp or {}).get("asset") or {}
    deps = ((pp or {}).get("assetVersionDetail") or {}).get("dependencies") or a.get("dependencies") or []
    conn.execute("DELETE FROM deps WHERE id=?", (mod_id,))
    rows = []
    for d in deps:
        asset = d.get("asset") or d
        did = (asset.get("id") or d.get("assetId") or "").upper()
        if did:
            rows.append((mod_id, did))
    if rows:
        conn.executemany("INSERT OR IGNORE INTO deps(id, dep) VALUES(?,?)", rows)


def _iso_to_ts(s):
    if not s:
        return 0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


SYNC = Sync()


def auto_sync_loop():
    """Live feed: every couple of minutes, pull whatever the Workshop changed since the last look
    (new mods and updates, newest first, stopping at the first page that is all old). Usually one
    or two page requests."""
    if int(CONFIG.get("auto_sync_minutes") or 0) <= 0:
        return
    secs = max(60, int(CONFIG.get("live_sync_seconds") or 120))
    while True:
        time.sleep(secs)
        SYNC.incremental()
        with db() as conn:
            # re-check downloads on anything updated since last time
            conn.execute("UPDATE mods SET downloads_at = 0 WHERE updated_at >= ?", (_days_ago_iso(1),))
        for _ in range(60):             # let the live check finish, then update what it found
            if not SYNC.running:
                break
            time.sleep(2)
        try:
            auto_update()
        except Exception as e:
            log("[update-mods]", e)


_backend_check = {"at": 0}


def backend_update_check(force=False):
    """Ask Bohemia's backend itself (through the hidden server tool) whether any installed Workshop
    mod has a newer version. The website can lag a new upload by hours; the backend knows at once.
    Runs at start-up, every 30 minutes, and from the Check for updates button."""
    if not CONFIG.get("auto_update", True) or not wsdl.server_exe():
        return {"ok": False, "error": "Server tool not installed"}
    if game_running():
        return {"ok": False, "error": "Close Arma first"}
    if wsdl.STATE.get("running"):
        return {"ok": False, "error": "A download is running"}
    if not force and time.time() - _backend_check["at"] < 1800:
        return {"ok": False, "error": "checked recently"}
    pinned = set()
    if party.MEMBER.get("on"):
        man = (party.MEMBER.get("info") or {}).get("manifest") or {}
        pinned = {m["guid"] for m in man.get("mods") or []}
    mods = [{"guid": g, "name": m.get("name") or g} for g, m in play.scan_mods().items()
            if m.get("kind") == "workshop" and g not in pinned]
    if not mods:
        return {"ok": True, "count": 0}
    _backend_check["at"] = time.time()
    r = wsdl.start(mods, check=True)
    log("[update-mods] backend check of", len(mods), "mod(s):", r)
    return dict(r, count=len(mods))


def _backend_check_loop():
    time.sleep(90)
    while True:
        try:
            backend_update_check()
        except Exception as e:
            log("[update-mods] backend check", e)
        time.sleep(300)


def _vtuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", str(v or "")))


def auto_update():
    """Steam-style: an installed Workshop mod with a newer version on the Workshop is updated in the
    background by the no-game downloader. Skipped while Arma runs (its files are in use), and for
    mods your party's host pins (the host's versions win there). Off with "auto_update": false."""
    if not CONFIG.get("auto_update", True) or not wsdl.server_exe() or game_running():
        return []
    pinned = set()
    if party.MEMBER.get("on"):
        man = (party.MEMBER.get("info") or {}).get("manifest") or {}
        pinned = {m["guid"] for m in man.get("mods") or []}
    installed = {g: m for g, m in play.scan_mods().items() if m.get("kind") == "workshop"}
    if not installed:
        return []
    ids = list(installed)
    with db() as conn:
        latest = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, name, version, size, thumb FROM mods WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()}
    todo = []
    for g, m in installed.items():
        lt = latest.get(g)
        if not lt or not lt["version"] or g in pinned:
            continue
        if _vtuple(lt["version"]) > _vtuple(m.get("version")):
            todo.append({"guid": g, "name": lt["name"] or m.get("name") or g, "version": lt["version"],
                         "size": lt["size"] or 0, "thumb": lt["thumb"] or ""})
    if todo:
        log("[update-mods]", ", ".join("%s %s -> %s" % (t["name"], installed[t["guid"]].get("version"), t["version"]) for t in todo))
        wsdl.start(todo)
    return todo


# ----------------------------------------------------------------------------
# installed mods (game addons folder)
# ----------------------------------------------------------------------------
_installed_cache = {"at": 0, "ids": set()}


def installed_ids():
    if time.time() - _installed_cache["at"] < 30:
        return _installed_cache["ids"]
    ids = set()
    dirs = [CONFIG.get("addons_dir") or ""] + list(CONFIG.get("addons_dirs_extra") or [])
    for d in dirs:
        if not d:
            continue
        try:
            for name in os.listdir(d):
                m = re.search(r"_([0-9A-F]{16})$", name)
                if m:
                    ids.add(m.group(1))
        except Exception:
            pass
    _installed_cache.update(at=time.time(), ids=ids)
    return ids


# ----------------------------------------------------------------------------
# queries
# ----------------------------------------------------------------------------
FRAMES = {"today": 1, "week": 7, "month": 30, "3months": 90, "6months": 180, "year": 365, "all": 0}


def query_mods(p):
    q = (p.get("q") or "").strip()
    sort = p.get("sort") or "popular"
    frame = p.get("frame") or "3months"
    tags = [t for t in (p.get("tags") or "").upper().split(",") if t]
    not_tags = [t for t in (p.get("not_tags") or "").upper().split(",") if t]
    min_votes = int(p.get("min_votes") or 0)
    min_rating = float(p.get("min_rating") or 0)
    max_mb = float(p.get("max_mb") or 0)
    hide_installed = p.get("hide_installed") == "1"
    only_installed = p.get("only_installed") == "1"
    only_listed = p.get("only_listed") == "1"
    only_favs = p.get("only_favs") == "1"
    playlist = int(p.get("playlist") or 0)
    author = (p.get("author") or "").strip()
    game_version = (p.get("game_version") or "").strip()
    page = max(1, int(p.get("page") or 1))
    per = min(100, max(10, int(p.get("per") or 50)))

    where = ["1=1"]
    args = []
    if q:
        where.append("(name LIKE ? OR summary LIKE ? OR author LIKE ? OR id = ?)")
        like = f"%{q}%"
        args += [like, like, like, q.upper()]
    for t in tags:
        where.append("(',' || tags || ',') LIKE ?")
        args.append(f"%,{t},%")
    for t in not_tags:
        where.append("(',' || tags || ',') NOT LIKE ?")
        args.append(f"%,{t},%")
    if min_votes:
        where.append("votes >= ?")
        args.append(min_votes)
    if min_rating:
        where.append("rating >= ?")
        args.append(min_rating)
    if max_mb:
        where.append("size <= ?")
        args.append(int(max_mb * 1024 * 1024))
    if author:
        where.append("author = ?")
        args.append(author)
    if game_version:
        where.append("game_version LIKE ?")
        args.append(game_version + "%")
    # "Filter by Date": updated / posted within N days
    upd_days = int(p.get("days") or 0)
    if upd_days > 0:
        where.append("updated_at >= ?")
        args.append(_days_ago_iso(upd_days))
    cr_days = int(p.get("created_days") or 0)
    if cr_days > 0:
        where.append("created_at >= ?")
        args.append(_days_ago_iso(cr_days))
    inst = installed_ids()
    if hide_installed and inst:
        where.append("id NOT IN (%s)" % ",".join("?" * len(inst)))
        args += list(inst)
    if only_installed:
        if inst:
            where.append("id IN (%s)" % ",".join("?" * len(inst)))
            args += list(inst)
        else:
            where.append("0")
    if only_listed:
        where.append("id IN (SELECT id FROM playlist_mods WHERE pid = ?)")
        args.append(playlist or active_playlist())
    if only_favs:
        where.append("id IN (SELECT id FROM favorites)")

    # time frame: "Most Popular (Three Months)" is the best rated among mods
    # posted in the window; Trending is what gained the most in the window.
    # Most Recent / Last Updated / Subscribers / Top Rated ignore it, as on Steam.
    if only_installed or only_listed or only_favs:
        frame = "all"   # "your items" are never time-framed
    days = FRAMES.get(frame, 90)
    since = _days_ago_iso(days) if days else None
    # custom date range (From / To): mods POSTED in it, or UPDATED in it for Last Updated.
    # Trending has no history that far back, so it ignores a range.
    d_from = _date_param(p.get("from"))
    d_to = _date_param(p.get("to"))
    if (d_from or d_to) and sort != "trending" and not (only_installed or only_listed or only_favs):
        col = "updated_at" if sort == "updated" else "created_at"
        if d_from:
            where.append(col + " >= ?")
            args.append(d_from.strftime("%Y-%m-%dT00:00:00.000Z"))
        if d_to:
            where.append(col + " < ?")
            args.append((d_to + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z"))
        since = None
    select_extra = ""
    if sort == "popular":
        # "Most Popular (One Week)" = the best of what was POSTED in that window
        if since:
            where.append("created_at >= ?")
            args.append(since)
        # Bayesian average toward 80% with 20 votes: 100% with 3 votes does
        # not beat 94% with 2,000
        # Bayesian rating (toward 80% with 20 votes) weighted by reach, so a
        # 96% mod with a million downloads outranks a 99% mod with 40
        order = "(((rating*votes + 16.0)/(votes+20.0)) * (1.0 + log10(COALESCE(downloads, subscribers, 0) + 10))) DESC, votes DESC"
    elif sort == "trending":
        # subscribers gained over the window, from the daily snapshots. New
        # database: nothing gained yet, falls back to popular order.
        day0 = datetime.fromtimestamp(time.time() - (days or 30) * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        select_extra = (", (COALESCE(downloads, subscribers, 0) - COALESCE((SELECT COALESCE(s.dl, s.subs) FROM stats s WHERE s.id = mods.id AND s.day >= ? "
                        "ORDER BY s.day LIMIT 1), COALESCE(downloads, subscribers, 0))) AS gain")
        args.insert(0, day0)
        order = "gain DESC, ((rating*votes + 16.0)/(votes+20.0)) DESC, COALESCE(downloads, subscribers, 0) DESC"
    elif sort == "rated":
        where.append("votes >= ?")
        args.append(max(min_votes, 5))
        order = "rating DESC, votes DESC"
    elif sort == "recent":
        order = "created_at DESC"
    elif sort == "updated":
        order = "updated_at DESC"
    elif sort == "subscribers":
        order = "COALESCE(downloads, subscribers, 0) DESC"
    elif sort == "size":
        order = "size DESC"
    elif sort == "name":
        order = "name COLLATE NOCASE ASC"
    else:
        order = "subscribers DESC"

    sql_where = " AND ".join(where)
    with db() as conn:
        count_args = args[1:] if sort == "trending" else args
        total = conn.execute(f"SELECT COUNT(*) FROM mods WHERE {sql_where}", count_args).fetchone()[0]
        rows = conn.execute(
            f"SELECT id,name,author,summary,rating,votes,subscribers,downloads,version,size,thumb,created_at,updated_at,tags,game_version{select_extra} "
            f"FROM mods WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [per, (page - 1) * per],
        ).fetchall()
        listed = {r["id"] for r in conn.execute("SELECT id FROM playlist_mods WHERE pid=?", (active_playlist(),)).fetchall()}
        favs = {r["id"] for r in conn.execute("SELECT id FROM favorites").fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        d["installed"] = r["id"] in inst
        d["listed"] = r["id"] in listed
        d["fav"] = r["id"] in favs
        out.append(d)
    return {"total": total, "page": page, "per": per, "rows": out}


def _date_param(v):
    """'YYYY-MM-DD' from the date pickers, or None."""
    try:
        return datetime.strptime((v or "").strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _days_ago_iso(days):
    return datetime.fromtimestamp(time.time() - days * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def facets():
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
        rows = conn.execute("SELECT tags FROM mods WHERE tags != ''").fetchall()
    counts = {}
    for r in rows:
        for t in r["tags"].split(","):
            counts[t] = counts.get(t, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    with db() as conn:
        hist = conn.execute("SELECT COUNT(DISTINCT day) FROM stats").fetchone()[0]
        enriched = conn.execute("SELECT COUNT(*) FROM mods WHERE downloads IS NOT NULL").fetchone()[0]
        favs = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
    return {"total": n, "tags": [{"tag": t, "count": c} for t, c in top if c >= 3][:400],
            "history_days": hist, "enriched": enriched, "favorites": favs,
            "installed": len(installed_ids()),
            "last_sync": float(meta_get("last_sync_at", 0) or 0),
            "full_sync": float(meta_get("full_sync_at", 0) or 0)}


def mod_detail(mod_id, refresh=False, max_age=600):
    """max_age: a mod page shown to you is at most 10 minutes old; dependency lookups accept a day.
    Either way it is refetched as soon as the list sees the mod was updated."""
    with db() as conn:
        r = conn.execute("SELECT id, updated_at FROM mods WHERE id=?", (mod_id,)).fetchone()
        dr = conn.execute("SELECT data, at FROM details WHERE id=?", (mod_id,)).fetchone()
    detail = None
    cached = None
    if dr:
        try:
            cached = detail_unpack(dr["data"])
        except Exception:
            cached = None
    at = (dr["at"] if dr else 0) or 0
    fresh = cached is not None and not refresh and time.time() - at < max_age \
        and _iso_to_ts(r["updated_at"] if r else "") <= at
    if fresh:
        detail = cached
    if detail is None:
        pp = SITE_API.detail(mod_id)
        if not pp and cached is not None:
            detail = cached     # the site is unreachable: show what we have
        if pp:
            detail = pp
            with db() as conn:
                conn.execute("INSERT OR REPLACE INTO details(id, data, at) VALUES(?,?,?)",
                             (mod_id, detail_pack(pp), time.time()))
                store_deps(conn, mod_id, pp)
                if not r:
                    # a mod we had not synced yet (opened by id)
                    a = pp.get("asset") or {}
                    if a.get("id"):
                        conn.execute(UPSERT, row_to_record(a, time.time()))
    if not detail:
        return None
    a = detail.get("asset") or {}
    deps = []
    for dep in (detail.get("assetVersionDetail") or {}).get("dependencies") or a.get("dependencies") or []:
        asset = dep.get("asset") or dep
        deps.append({"id": asset.get("id") or dep.get("assetId"), "name": asset.get("name") or dep.get("name") or "",
                     "version": dep.get("version") or asset.get("currentVersionNumber") or ""})
    shots = [s.get("url") for s in (a.get("screenshots") or []) if s.get("url")]
    prev = [p.get("url") for p in (a.get("previews") or []) if p.get("url")]
    ratings = a.get("ratings") or {}
    dl = (detail.get("getAssetDownloadTotal") or {}).get("total")
    out = {
        "id": a.get("id"), "name": a.get("name"), "author": (a.get("author") or {}).get("username"),
        "description": a.get("description") or a.get("summary") or "",
        "summary": a.get("summary") or "",
        "version": a.get("currentVersionNumber"), "size": a.get("currentVersionSize"),
        "game_version": a.get("gameVersion"),
        "likes": ratings.get("likes"), "dislikes": ratings.get("dislikes"),
        "rating": a.get("averageRating"), "votes": a.get("ratingCount"),
        "subscribers": a.get("subscriberCount"), "downloads": dl,
        "created_at": a.get("createdAt"), "updated_at": a.get("updatedAt"),
        "tags": [t.get("name") for t in (a.get("tags") or [])],
        "license": a.get("license"),
        "dependencies": deps,
        "scenarios": [{"name": s.get("name"), "players": s.get("playerCount")} for s in (a.get("scenarios") or []) if isinstance(s, dict)],
        "screenshots": prev + shots,
        "changelog": (detail.get("assetVersionDetail") or {}).get("changelog") or "",
        "versions": [{"version": v.get("version"), "at": v.get("createdAt"), "game": v.get("gameVersion")} for v in (a.get("versions") or [])[:10]],
        "url": f"{SITE}/workshop/{a.get('id')}",
        "installed": (a.get("id") in installed_ids()),
        "installed_version": ((play.scan_mods().get((a.get("id") or "").upper()) or {}).get("version") or ""),
    }
    with db() as conn:
        out["fav"] = bool(conn.execute("SELECT 1 FROM favorites WHERE id=?", (a.get("id"),)).fetchone())
    return out


def deps_tree(mod_id, max_depth=5):
    """Everything mod_id requires, recursively, as a flat list. Each mod's own
    page carries its dependency tree; nested trees can be empty on the site
    even when the dependency has dependencies, so each one is looked up."""
    seen = {mod_id.upper()}
    out = []
    frontier = [(mod_id.upper(), 0)]
    while frontier:
        cur, depth = frontier.pop(0)
        d = mod_detail(cur)
        if not d:
            continue
        for dep in d.get("dependencies") or []:
            did = (dep.get("id") or "").upper()
            if not did or did in seen:
                continue
            seen.add(did)
            with db() as conn:
                r = conn.execute("SELECT name, author, size, thumb FROM mods WHERE id=?", (did,)).fetchone()
            out.append({"id": did, "name": dep.get("name") or (r["name"] if r else did),
                        "author": r["author"] if r else "", "size": r["size"] if r else 0,
                        "thumb": r["thumb"] if r else "", "depth": depth + 1})
            if depth + 1 < max_depth:
                frontier.append((did, depth + 1))
    return out


# ----------------------------------------------------------------------------
# favorites and playlists (what gets handed to the game)
# ----------------------------------------------------------------------------
def active_playlist():
    v = meta_get("active_playlist")
    try:
        return int(v)
    except (TypeError, ValueError):
        with db() as conn:
            r = conn.execute("SELECT pid FROM playlists ORDER BY pid LIMIT 1").fetchone()
        return r["pid"] if r else 0


def playlists_get():
    with db() as conn:
        rows = conn.execute(
            "SELECT p.pid, p.name, COUNT(m.id) AS n, COALESCE(SUM(x.size),0) AS size FROM playlists p "
            "LEFT JOIN playlist_mods m ON m.pid=p.pid LEFT JOIN mods x ON x.id=m.id GROUP BY p.pid ORDER BY p.name COLLATE NOCASE").fetchall()
    return {"active": active_playlist(), "playlists": [dict(r) for r in rows]}


def playlist_items(pid):
    with db() as conn:
        rows = conn.execute(
            "SELECT l.id, l.added_at, m.name, m.author, m.size, m.version, m.thumb FROM playlist_mods l LEFT JOIN mods m ON m.id=l.id WHERE l.pid=? ORDER BY l.added_at",
            (pid,)).fetchall()
    inst = installed_ids()
    return [dict(r, installed=(r["id"] in inst)) for r in rows]


def playlist_create(name):
    """A new playlist; a taken name gets a (2), (3)... suffix instead of
    silently returning the existing one."""
    base = (name or "").strip()[:56] or "Playlist"
    with db() as conn:
        name, n = base, 2
        while conn.execute("SELECT 1 FROM playlists WHERE name=?", (name,)).fetchone():
            name = "%s (%d)" % (base, n)
            n += 1
        conn.execute("INSERT INTO playlists(name, created_at) VALUES(?,?)", (name, time.time()))
        r = conn.execute("SELECT pid FROM playlists WHERE name=?", (name,)).fetchone()
    return r["pid"]


def playlist_rename(pid, name):
    name = (name or "").strip()[:60] or "Playlist"
    with db() as conn:
        if conn.execute("SELECT 1 FROM playlists WHERE name=? AND pid<>?", (name, pid)).fetchone():
            return False
        conn.execute("UPDATE playlists SET name=? WHERE pid=?", (name, pid))
    if pid == active_playlist():
        write_queue()   # the name is in the file
    return True


def playlist_delete(pid):
    was_active = (active_playlist() == pid)
    with db() as conn:
        conn.execute("DELETE FROM playlist_mods WHERE pid=?", (pid,))
        conn.execute("DELETE FROM playlists WHERE pid=?", (pid,))
        if conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 0:
            conn.execute("INSERT INTO playlists(name, created_at) VALUES('Default', ?)", (time.time(),))
        r = conn.execute("SELECT pid FROM playlists ORDER BY pid LIMIT 1").fetchone()
    if was_active:
        meta_set("active_playlist", r["pid"])
        write_queue()


def playlist_duplicate(pid, name):
    new = playlist_create(name)
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO playlist_mods(pid,id,added_at) SELECT ?, id, added_at FROM playlist_mods WHERE pid=?", (new, pid))
    return new


def playlist_add(pid, mod_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO playlist_mods(pid, id, added_at) VALUES(?,?,?)", (pid, mod_id, time.time()))
        conn.execute("DELETE FROM removals WHERE id=?", (mod_id,))
    if pid == active_playlist():
        write_queue()


def playlist_remove(pid, mod_id):
    with db() as conn:
        conn.execute("DELETE FROM playlist_mods WHERE pid=? AND id=?", (pid, mod_id))
    if pid == active_playlist():
        write_queue()


def playlist_clear(pid):
    with db() as conn:
        conn.execute("DELETE FROM playlist_mods WHERE pid=?", (pid,))
    if pid == active_playlist():
        write_queue()


def playlist_activate(pid):
    meta_set("active_playlist", pid)
    write_queue()


def auto_download(ids, look_up=True):
    """Subscribing downloads, like Steam: whatever was just added (and everything it needs)
    that is not on this PC goes to the no-game downloader. Off with "auto_download": false."""
    if not CONFIG.get("auto_download", True) or not wsdl.server_exe():
        return
    want = {i: "" for i in ids if i}
    for did, name in _closure_from_db(list(want)).items():
        want.setdefault(did, name)
    _installed_cache["at"] = 0
    inst = installed_ids()
    local = play.local_guids()   # yours, not on the Workshop
    blocked = _deleted()   # deleted by you: never fetched again unless you subscribe to it yourself
    todo = [g for g in want if g not in inst and g not in local and g not in blocked]
    if not todo:
        return
    with db() as conn:
        meta = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, name, size, thumb FROM mods WHERE id IN (%s)" % ",".join("?" * len(todo)), todo).fetchall()}
    mods = [{"guid": g, "name": (meta.get(g) or {}).get("name") or g, "size": (meta.get(g) or {}).get("size") or 0,
             "thumb": (meta.get(g) or {}).get("thumb") or ""} for g in todo]
    r = wsdl.start(mods)
    log("[download] subscribe ->", len(mods), "mod(s):", r)
    # dependencies nobody has fetched details for yet: fetch them, then download what they need
    unknown = [g for g in want if g not in meta]
    if unknown and look_up:
        threading.Thread(target=lambda: (_fetch_details_then_rewrite(unknown), auto_download(unknown, False)), daemon=True).start()


def subscribed_ids():
    """Everything on any playlist, plus everything those mods need."""
    with db() as conn:
        ids = {r["id"] for r in conn.execute("SELECT DISTINCT id FROM playlist_mods").fetchall()}
    return ids | set(_closure_from_db(list(ids)))


def uninstall_unsubscribed(ids):
    """Unsubscribing removes the files, like Steam: a Workshop mod that is no longer on any
    playlist and that no subscribed mod needs is deleted from the addons folder. Skipped
    while the game runs (its files are in use). Returns the names removed."""
    import shutil
    if not CONFIG.get("uninstall_on_unsubscribe", True) or not ids:
        return []
    if game_running():
        log("[unsubscribe] game is running; files kept for now:", ids)
        return []
    keep = subscribed_ids()
    d = CONFIG.get("addons_dir") or ""
    # never your own work: any GUID that belongs to a local project (Workbench or localmods) is
    # off limits, and only the game's Workshop download folder is ever touched
    local = play.local_guids() | {g for g, m in play.scan_mods().items() if m["kind"] == "local"}
    removed = []
    for g in ids:
        g = (g or "").upper()
        if not g or g in keep or g in local:
            continue
        for n in play._listdir(d):
            if n.upper().endswith("_" + g):
                try:
                    shutil.rmtree(os.path.join(d, n))
                    play.invalidate_scan()
                    removed.append(n)
                    log("[unsubscribe] deleted", n)
                except Exception as e:
                    log("[unsubscribe] could not delete", n, e)
    _installed_cache["at"] = 0
    if removed:
        try:
            seen = set(json.loads(meta_get("seen_installed") or "[]"))
            meta_set("seen_installed", json.dumps(sorted(seen - {g.upper() for g in ids})))
        except ValueError:
            pass
    return removed


def sync_with_disk():
    """The addons folder is the truth. A mod that was on this PC before and is gone now was
    deleted on purpose (the game's own mod manager, or by hand), so it is unsubscribed from
    every playlist and never downloaded again. A mod that shows up (downloaded by the game)
    goes onto the active playlist. Mods subscribed here but never on disk yet are left for
    the downloader. Only a folder that is gone counts as deleted; a half-updated one does not."""
    _installed_cache["at"] = 0
    inst = set(installed_ids())
    raw = meta_get("seen_installed")
    try:
        seen = set(json.loads(raw)) if raw else None
    except ValueError:
        seen = None
    busy = {m["guid"] for m in wsdl.STATE.get("mods") or []} | {m["guid"] for m in wsdl.PENDING}
    gone = sorted((seen or set()) - inst - busy)
    if gone:
        with db() as conn:
            rows = conn.execute("SELECT pid, id FROM playlist_mods WHERE id IN (%s)" % ",".join("?" * len(gone)), gone).fetchall()
        for r in rows:
            playlist_remove(r["pid"], r["id"])
        if rows:
            log("[sync] deleted outside the launcher, unsubscribed:", ", ".join(sorted({r["id"] for r in rows})))
        _deleted_add(gone)
    meta_set("seen_installed", json.dumps(sorted(inst | ((seen or set()) & busy))))
    if seen is not None and inst - seen:
        import_installed()   # new downloads made by the game


def _deleted():
    try:
        return set(json.loads(meta_get("user_deleted") or "[]"))
    except ValueError:
        return set()


def _deleted_add(ids):
    meta_set("user_deleted", json.dumps(sorted(_deleted() | {i.upper() for i in ids})))


def _deleted_forget(ids):
    d = _deleted()
    if d & {i.upper() for i in ids}:
        meta_set("user_deleted", json.dumps(sorted(d - {i.upper() for i in ids})))


_disk_watch = [False]


def download_missing_subscriptions():
    """At start: reconcile with the addons folder, then download anything subscribed that is
    not on the PC (Steam does the same). Keeps watching the folder while the browser is open."""
    try:
        sync_with_disk()
        auto_download(sorted(subscribed_ids()))
    except Exception as e:
        log("[download] startup check failed", e)
    if not _disk_watch[0]:
        _disk_watch[0] = True

        def watch():
            while True:
                time.sleep(20)
                try:
                    sync_with_disk()
                except Exception as e:
                    log("[sync] check failed", e)
        threading.Thread(target=watch, daemon=True).start()


def library():
    """Everything the My Mods tab shows: what is on this PC (Workshop downloads and your
    own projects), favorites, and every playlist with its mods."""
    mods = play.scan_mods()
    ws_ids = [g for g, m in mods.items() if m["kind"] == "workshop"]
    with db() as conn:
        pls = [dict(r) for r in conn.execute("SELECT pid, name FROM playlists ORDER BY name COLLATE NOCASE").fetchall()]
        members = {}
        for r in conn.execute("SELECT pid, id, added_at FROM playlist_mods").fetchall():
            members.setdefault(r["id"], []).append(r["pid"])
        favs = {r["id"] for r in conn.execute("SELECT id FROM favorites").fetchall()}
        want = set(ws_ids) | set(members) | favs
        meta = {}
        if want:
            ids = list(want)
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                for r in conn.execute("SELECT id, name, author, size, version, thumb, rating, votes, tags, updated_at FROM mods WHERE id IN (%s)"
                                      % ",".join("?" * len(chunk)), chunk).fetchall():
                    meta[r["id"]] = dict(r)
    local_img = {}
    for g, m in mods.items():
        f = play._find_image(play._image_folders(m), "thumb")
        if f:
            play.IMAGES["thumb:" + g] = f
            local_img[g] = "/api/play/img/thumb/" + g
    items = {}
    for g in want:
        m = meta.get(g) or {}
        loc = mods.get(g)
        items[g] = {"id": g, "name": m.get("name") or (loc or {}).get("name") or g, "author": m.get("author") or "",
                    "size": m.get("size") or 0, "version": (loc or {}).get("version") or m.get("version") or "",
                    "latest": m.get("version") or "", "thumb": m.get("thumb") or local_img.get(g, ""), "rating": m.get("rating"),
                    "votes": m.get("votes") or 0, "tags": m.get("tags") or "", "updated_at": m.get("updated_at") or "",
                    "installed": bool(loc), "kind": "workshop", "fav": g in favs, "playlists": members.get(g, [])}
    for g, m in mods.items():
        if m["kind"] == "local":
            items[g] = {"id": g, "name": m["name"], "author": "you", "size": 0, "version": "", "latest": "",
                        "thumb": local_img.get(g, ""),
                        "installed": True, "kind": "local", "build": m.get("build"), "stale": m.get("stale"),
                        "fav": g in favs, "playlists": members.get(g, []), "rating": None, "votes": 0, "tags": "", "updated_at": ""}
    for p_ in pls:
        p_["ids"] = [g for g, pids in members.items() if p_["pid"] in pids]
    return {"items": list(items.values()), "playlists": pls, "active": active_playlist()}


def fav_toggle(mod_id):
    with db() as conn:
        if conn.execute("SELECT 1 FROM favorites WHERE id=?", (mod_id,)).fetchone():
            conn.execute("DELETE FROM favorites WHERE id=?", (mod_id,))
            return False
        conn.execute("INSERT INTO favorites(id, added_at) VALUES(?,?)", (mod_id, time.time()))
        return True


def removals_get():
    with db() as conn:
        rows = conn.execute("SELECT r.id, COALESCE(m.name, r.name) AS name, m.thumb, m.author, m.size, r.queued_at "
                            "FROM removals r LEFT JOIN mods m ON m.id=r.id ORDER BY r.queued_at").fetchall()
    inst = installed_ids()
    return [dict(r, installed=(r["id"] in inst)) for r in rows]


def removal_add(mod_id):
    """Ask the game to delete an installed mod from disk. It also leaves the
    active playlist, otherwise the companion would download it right back."""
    with db() as conn:
        r = conn.execute("SELECT name FROM mods WHERE id=?", (mod_id,)).fetchone()
        conn.execute("INSERT OR REPLACE INTO removals(id, name, queued_at) VALUES(?,?,?)",
                     (mod_id, r["name"] if r else "", time.time()))
        conn.execute("DELETE FROM playlist_mods WHERE pid=? AND id=?", (active_playlist(), mod_id))
    write_queue()


def removal_cancel(mod_id):
    with db() as conn:
        conn.execute("DELETE FROM removals WHERE id=?", (mod_id,))
    write_queue()


def removals_prune():
    """Drop removal requests for mods that are gone from disk."""
    inst = installed_ids()
    with db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM removals").fetchall()]
        gone = [i for i in ids if i not in inst]
        if gone:
            conn.executemany("DELETE FROM removals WHERE id=?", [(i,) for i in gone])
    return bool(gone)


def import_installed():
    """Everything downloaded in the game's addons folder that is on no playlist
    goes onto the active one, so the browser matches what is on disk. Runs at
    every start (and from the gear menu). Returns the number added."""
    _installed_cache["at"] = 0
    inst = installed_ids()
    if not inst:
        return 0
    pid = active_playlist()
    with db() as conn:
        listed = {r["id"] for r in conn.execute("SELECT DISTINCT id FROM playlist_mods").fetchall()}
        removing = {r["id"] for r in conn.execute("SELECT id FROM removals").fetchall()}
        known = {r["id"] for r in conn.execute("SELECT id FROM mods WHERE id IN (%s)" % ",".join("?" * len(inst)), list(inst)).fetchall()}
    new = [i for i in inst if i not in listed and i not in removing]
    if not new:
        return 0
    with db() as conn:
        conn.executemany("INSERT OR IGNORE INTO playlist_mods(pid, id, added_at) VALUES(?,?,?)",
                         [(pid, i, time.time()) for i in new])
    log("[installed] imported", len(new), "mod(s) from the addons folder into the active playlist")
    unknown = [i for i in new if i not in known]
    if unknown:
        threading.Thread(target=_fetch_details_then_rewrite, args=(unknown,), daemon=True).start()
    write_queue()
    return len(new)


_queue_lock = threading.Lock()
_queue_timer = [None]


def write_queue(delay=0.4):
    """Debounced: the file is rebuilt shortly after the last change."""
    with _queue_lock:
        if _queue_timer[0]:
            _queue_timer[0].cancel()
        t = threading.Timer(delay, _write_queue_now)
        t.daemon = True
        _queue_timer[0] = t
        t.start()


def _closure_from_db(ids):
    """Dependencies of ids, recursively, from the deps table only (no network)."""
    seen = set(ids)
    frontier = list(ids)
    out = {}
    with db() as conn:
        while frontier:
            cur = frontier.pop()
            for r in conn.execute("SELECT d.dep, m.name FROM deps d LEFT JOIN mods m ON m.id=d.dep WHERE d.id=?", (cur,)).fetchall():
                did = (r["dep"] or "").upper()
                if did and did not in seen:
                    seen.add(did)
                    out[did] = r["name"] or ""
                    frontier.append(did)
    return out


def _fetch_details_then_rewrite(ids):
    """Mods with no details yet get their dependency tree fetched, then the
    queue is written again with the closure."""
    changed = False
    for mid in ids:
        try:
            if mod_detail(mid):
                changed = True
        except Exception as e:
            log("[queue] detail fetch failed for", mid, e)
    if changed:
        write_queue(0)


def _write_queue_now():
    try:
        _write_queue_inner()
    except Exception as e:
        import traceback
        log("[queue] write failed:", e, traceback.format_exc())


def _write_queue_inner():
    """$profile:BetterWorkshop/queue.txt: the active playlist plus everything it
    depends on, and the mods to delete. The in-game companion downloads what is
    missing, enables exactly these mods, disables the rest, deletes the '-' ones.

        playlist=<name>
        written=<stamp>          changes only when the content changes
        +<ID> <name>
        -<ID> <name>
    """
    qdir = CONFIG.get("queue_dir")
    if not qdir:
        return
    if party.WS.get("running"):
        return   # a party Workshop download owns queue.txt right now
    pid = active_playlist()
    with db() as conn:
        r = conn.execute("SELECT name FROM playlists WHERE pid=?", (pid,)).fetchone()
    items = playlist_items(pid)
    want = {}
    for i in items:
        want[i["id"]] = i["name"] or ""
    # dependency closure from what is already known; unknown trees are fetched
    # in the background and the file is rewritten when they arrive
    for did, name in _closure_from_db(list(want)).items():
        want.setdefault(did, name)
    with db() as conn:
        missing = [r["id"] for r in conn.execute(
            "SELECT id FROM mods WHERE id IN (%s) AND id NOT IN (SELECT id FROM details)" % ",".join("?" * len(want)), list(want)).fetchall()] if want else []
        known = {r["id"] for r in conn.execute("SELECT id FROM mods WHERE id IN (%s)" % ",".join("?" * len(want)), list(want)).fetchall()} if want else set()
        missing += [i for i in want if i not in known]
    if missing:
        threading.Thread(target=_fetch_details_then_rewrite, args=(missing,), daemon=True).start()
    with db() as conn:
        rem = [(x["id"], x["name"] or "") for x in conn.execute(
            "SELECT r.id, COALESCE(m.name, r.name) AS name FROM removals r LEFT JOIN mods m ON m.id=r.id").fetchall()]
    rem = [(i, n) for i, n in rem if i not in want]
    body = ["+%s %s" % (i, n) for i, n in sorted(want.items())] + ["-%s %s" % (i, n) for i, n in sorted(rem)]
    nonce = meta_get("queue_nonce") or ""
    stamp = hashlib.sha1(("\n".join(body) + "|" + (r["name"] if r else "") + "|" + nonce).encode("utf-8")).hexdigest()[:16]
    text = "playlist=%s\nwritten=%s\n%s\n" % (r["name"] if r else "", stamp, "\n".join(body))
    for d in queue_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "queue.txt")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, path)
        except Exception as e:
            log("[queue] could not write", d, e)
    meta_set("queue_stamp", stamp)
    log("[queue] wrote", stamp, len(want), "mods,", len(rem), "deletions ->", ", ".join(queue_dirs()))


def queue_force():
    """Ask the game to apply the playlist again even if nothing changed."""
    meta_set("queue_nonce", str(int(time.time())))
    write_queue(0)


def game_status():
    """What the companion mod last reported (status.txt / applied.txt)."""
    qdir = status_dir()
    out = {"stamp": meta_get("queue_stamp") or "", "applied": "", "state": "", "phase": 0,
           "downloads": 0, "downloads_running": 0, "download_progress": 0.0, "lookups_left": 0,
           "failed": [], "status_at": 0, "queue_at": 0, "game_running": game_running()}
    try:
        out["queue_at"] = os.path.getmtime(os.path.join(qdir, "queue.txt"))
    except OSError:
        pass
    try:
        with open(os.path.join(qdir, "applied.txt"), "r", encoding="utf-8", errors="replace") as f:
            out["applied"] = f.readline().strip()
    except OSError:
        pass
    try:
        sp = os.path.join(qdir, "status.txt")
        out["status_at"] = os.path.getmtime(sp)
        with open(sp, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == "failed":
                    out["failed"].append(v)
                elif k in ("phase", "downloads", "downloads_running", "lookups_left"):
                    out[k] = int(v or 0)
                elif k == "download_progress":
                    out[k] = float(v or 0)
                elif k in ("state", "stamp"):
                    out["status_stamp" if k == "stamp" else k] = v
    except (OSError, ValueError):
        pass
    if out.get("status_stamp") != out["stamp"]:
        # status is about an older queue file
        out["state"] = ""
    out["up_to_date"] = (out["applied"] == out["stamp"] and bool(out["stamp"]))
    if removals_prune():
        write_queue(0)
    with db() as conn:
        out["removal_ids"] = [r["id"] for r in conn.execute("SELECT id FROM removals").fetchall()]
    return out


def sync_local_mods():
    """Mirror the dev projects listed in local_mods_src into local_mods_dir
    (what the game gets as -addonsDir). Skips sources that do not exist."""
    dst_root = CONFIG.get("local_mods_dir") or ""
    srcs = CONFIG.get("local_mods_src") or []
    if not dst_root or not srcs:
        return
    import shutil
    try:
        os.makedirs(dst_root, exist_ok=True)
    except Exception as e:
        log("[localmods] cannot create", dst_root, e)
        return
    for src in srcs:
        if not os.path.isdir(src):
            log("[localmods] missing source", src)
            continue
        dst = os.path.join(dst_root, os.path.basename(src.rstrip("\\/")))
        try:
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.pak", "*_manifest.json", "ServerData.json"))
            log("[localmods] mirrored", src, "->", dst)
        except Exception as e:
            log("[localmods] copy failed", src, e)


def launch_game():
    """Start Arma Reforger through Steam (so it logs in and has its launch options)."""
    if game_running():
        return "running"
    if os.name != "nt":
        return "not windows"
    sync_local_mods()
    # Start the game with the companion loaded at the main menu, so the playlist gets
    # downloaded and applied there. Falls back to a plain Steam start without it.
    try:
        cdir, g = party.companion_dir(), play.game_dir()
        if cdir and g:
            import subprocess
            args = [os.path.join(g, "ArmaReforgerSteam.exe"), "-addonsDir", cdir, "-addons", party.COMPANION_GUID]
            err = play.ensure_steam()
            if err:
                raise RuntimeError(err)
            play.start_game(args, g)
            _game_running_cache["at"] = 0
            log("[game] launched with the companion:", subprocess.list2cmdline(args))
            return "launched"
    except Exception as e:
        log("[game] direct launch failed, using Steam:", e)
    try:
        os.startfile("steam://rungameid/%s" % CONFIG.get("steam_app_id", 1874880))
        _game_running_cache["at"] = 0
        log("[game] launch requested through Steam")
        return "launched"
    except Exception as e:
        log("[game] could not launch:", e)
        return "error: %s" % e


_game_running_cache = {"at": 0, "v": False}
UI_SEEN = [0.0]   # when the app window last polled; the launcher uses it to tell the window is open


def game_running():
    if time.time() - _game_running_cache["at"] < 5:
        return _game_running_cache["v"]
    v = False
    if os.name == "nt":
        try:
            import subprocess
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ArmaReforgerSteam.exe", "/NH"],
                               capture_output=True, text=True, timeout=5, creationflags=0x08000000)
            v = "ArmaReforgerSteam.exe" in (r.stdout or "")
        except Exception:
            v = False
    _game_running_cache.update(at=time.time(), v=v)
    return v


def close_game():
    """Shut the game down the way Steam's Stop does: force-kill it and everything it started."""
    if os.name != "nt":
        return {"ok": False, "error": "Windows only."}
    import subprocess
    for exe in ("ArmaReforgerSteam.exe", "ArmaReforgerSteamDiag.exe", "ArmaReforger_BE.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/IM", exe], capture_output=True, timeout=20, creationflags=0x08000000)
        except Exception as e:
            log("[game] close", exe, e)
    _game_running_cache.update(at=0, v=False)
    log("[game] closed by the Close Arma button")
    return {"ok": True, "running": game_running()}


# ----------------------------------------------------------------------------
# http
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        p = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "ui.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html")
            if u.path.startswith("/snd/"):
                name = os.path.basename(u.path[5:])
                sp = os.path.join(HERE, "sounds", name)
                if name.endswith(".mp3") and os.path.isfile(sp):
                    with open(sp, "rb") as f:
                        return self._send(200, f.read(), "audio/mpeg")
                return self._send(404, {"error": "no sound"})
            if u.path == "/banner.jpg":
                bp = os.path.join(HERE, "banner.jpg")
                if os.path.exists(bp):
                    with open(bp, "rb") as f:
                        return self._send(200, f.read(), "image/jpeg")
                return self._send(404, {"error": "no banner"})
            if u.path in ("/favicon.ico", "/icon.ico"):
                ico = os.path.join(HERE, "icon.ico")
                if os.path.exists(ico):
                    with open(ico, "rb") as f:
                        return self._send(200, f.read(), "image/x-icon")
                return self._send(404, {"error": "no icon"})
            if u.path == "/api/mods":
                return self._send(200, query_mods(p))
            if u.path == "/api/facets":
                return self._send(200, facets())
            if u.path.startswith("/api/mod/"):
                mid = u.path.rsplit("/", 1)[1].upper()
                d = mod_detail(mid, refresh=p.get("refresh") == "1")
                return self._send(200 if d else 404, d or {"error": "not found"})
            if u.path == "/api/playlists":
                return self._send(200, playlists_get())
            if u.path == "/api/required":
                with db() as conn:
                    rows = conn.execute(
                        "SELECT m.id, m.name, m.author, m.thumb, m.rating, m.votes, m.downloads, m.size, COUNT(d.id) AS dependents "
                        "FROM deps d JOIN mods m ON m.id = d.dep GROUP BY d.dep ORDER BY dependents DESC LIMIT 10").fetchall()
                return self._send(200, [dict(r) for r in rows])
            if u.path == "/api/updates":
                inst = list(installed_ids())
                if not inst:
                    return self._send(200, [])
                with db() as conn:
                    rows = conn.execute(
                        "SELECT id, name, author, thumb, version, updated_at, size FROM mods WHERE id IN (%s) ORDER BY updated_at DESC LIMIT 8"
                        % ",".join("?" * len(inst)), inst).fetchall()
                return self._send(200, [dict(r) for r in rows])
            if u.path.startswith("/api/deps/"):
                mid = u.path.rsplit("/", 1)[1].upper()
                pid = int(p.get("pid") or 0) or active_playlist()
                deps = deps_tree(mid)
                inst = installed_ids()
                with db() as conn:
                    inpl = {r["id"] for r in conn.execute("SELECT id FROM playlist_mods WHERE pid=?", (pid,)).fetchall()}
                for d in deps:
                    d["installed"] = d["id"] in inst
                    d["in_playlist"] = d["id"] in inpl
                return self._send(200, {"deps": deps})
            if u.path.startswith("/api/playlist/"):
                pid = int(u.path.rsplit("/", 1)[1])
                return self._send(200, playlist_items(pid))
            if u.path == "/api/status":
                UI_SEEN[0] = time.time()
                return self._send(200, {"sync": SYNC.status, "running": SYNC.running, "config": CONFIG,
                                        "enrich": {"running": ENRICH.running, "done": ENRICH.done, "todo": ENRICH.todo},
                                        "mods": facets()["total"], "game": game_status()})
            if u.path == "/api/game":
                return self._send(200, {"game": game_status(), "removals": removals_get(),
                                        "companion": os.path.exists(os.path.join(status_dir(), "status.txt"))})
            if u.path == "/api/removals":
                return self._send(200, removals_get())
            if u.path == "/api/play/state":
                st = play.state()
                ids = [m["guid"] for m in st["mods"]]
                if ids:
                    with db() as conn:
                        meta = {r["id"]: dict(r) for r in conn.execute(
                            "SELECT id, author, size, thumb FROM mods WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()}
                    for m in st["mods"]:
                        x = meta.get(m["guid"]) or {}
                        m["author"] = x.get("author") or ""
                        m["size"] = x.get("size") or 0
                        if not m.get("img") and x.get("thumb"):
                            m["img"] = x["thumb"]
                # dependencies that are not on this PC (yet): names, and which ones are downloading
                have = set(ids)
                need = sorted({d for m in st["mods"] for d in (m.get("deps") or []) if d not in have})
                st["dep_names"] = {}
                if need:
                    with db() as conn:
                        st["dep_names"] = {r["id"]: r["name"] for r in conn.execute(
                            "SELECT id, name FROM mods WHERE id IN (%s)" % ",".join("?" * len(need)), need).fetchall()}
                v = party.downloads_view()
                st["downloading"] = {r["guid"]: r["name"] for r in v["active"] + v["queued"]}
                return self._send(200, st)
            if u.path == "/api/play/downloading":
                v = party.downloads_view()
                return self._send(200, {"downloading": {r["guid"]: r["name"] for r in v["active"] + v["queued"]}})
            if u.path.startswith("/api/play/img/"):
                parts = u.path.split("/")          # /api/play/img/<thumb|scen>/<GUID>
                pth = play.image_path("%s:%s" % (parts[4], parts[5].upper())) if len(parts) > 5 else ""
                if not pth:
                    return self._send(404, {"error": "no image"})
                with open(pth, "rb") as f:
                    data = f.read()
                ct = "image/png" if pth.lower().endswith(".png") else "image/jpeg"
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/play/start":
                return self._send(200, play.START)
            if u.path == "/api/play/build":
                return self._send(200, play.BUILD)
            if u.path == "/api/library":
                return self._send(200, library())
            if u.path == "/api/app/version":
                return self._send(200, {"version": update.running_version()})
            if u.path == "/api/update":
                return self._send(200, update.status())
            if u.path == "/api/downloads":
                v = party.downloads_view()
                ids = [r["guid"] for r in v["active"] + v["queued"] + v["history"]]
                if ids:
                    with db() as conn:
                        th = {r["id"]: (r["thumb"], r["size"]) for r in conn.execute(
                            "SELECT id, thumb, size FROM mods WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()}
                    for r in v["active"] + v["queued"] + v["history"]:
                        t, sz = th.get(r["guid"], ("", 0))
                        r["thumb"] = r.get("thumb") or t or ""
                        if not r.get("size") and sz:
                            r["size"] = sz
                return self._send(200, v)
            if u.path == "/api/download":
                return self._send(200, wsdl.status())
            if u.path == "/api/party":
                party.set_game_running(game_running)
                return self._send(200, {"host": party.host_status(), "member": party.member_status(), "ws": party.ws_status()})
            return self._send(404, {"error": "no such route"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        try:
            pid = int(body.get("pid") or 0) or active_playlist()
            mid = str(body.get("id", "")).upper()
            if u.path == "/api/playlist/add":
                playlist_add(pid, mid)
                for extra in body.get("also") or []:
                    playlist_add(pid, str(extra).upper())
                _deleted_forget([mid] + [str(x).upper() for x in body.get("also") or []])
                auto_download([mid] + [str(x).upper() for x in body.get("also") or []])
                return self._send(200, playlist_items(pid))
            if u.path == "/api/playlist/remove":
                playlist_remove(pid, mid)
                uninstall_unsubscribed([mid])
                return self._send(200, playlist_items(pid))
            if u.path == "/api/unsubscribe_all":
                # every Workshop mod: off every playlist, marked as deleted by you, files removed.
                # Local projects are never touched.
                mods = play.scan_mods()
                local = play.local_guids() | {g for g, m in mods.items() if m["kind"] == "local"}
                with db() as conn:
                    rows = conn.execute("SELECT pid, id FROM playlist_mods").fetchall()
                ws_ids = {g for g, m in mods.items() if m["kind"] == "workshop"} | {r["id"] for r in rows if r["id"] not in local}
                ws_ids -= local
                for r in rows:
                    if r["id"] in ws_ids:
                        playlist_remove(r["pid"], r["id"])
                wsdl.stop()
                del wsdl.PENDING[:]
                _deleted_add(list(ws_ids))
                removed = uninstall_unsubscribed(list(ws_ids))
                log("[unsubscribe] all Workshop mods:", len(ws_ids), "unsubscribed,", len(removed), "deleted")
                return self._send(200, {"unsubscribed": len(ws_ids), "removed": removed,
                                        "kept_running": game_running()})
            if u.path == "/api/unsubscribe":
                # off every playlist and off the PC
                with db() as conn:
                    pids = [r["pid"] for r in conn.execute("SELECT pid FROM playlist_mods WHERE id=?", (mid,)).fetchall()]
                for p_ in pids:
                    playlist_remove(p_, mid)
                _deleted_add([mid])
                return self._send(200, {"removed": uninstall_unsubscribed([mid])})
            if u.path == "/api/playlist/clear":
                gone = [i["id"] for i in playlist_items(pid)]
                playlist_clear(pid)
                uninstall_unsubscribed(gone)
                return self._send(200, [])
            if u.path == "/api/playlist/create":
                return self._send(200, {"pid": playlist_create(body.get("name"))})
            if u.path == "/api/playlist/rename":
                if not playlist_rename(pid, body.get("name")):
                    return self._send(200, {"error": "A playlist with that name already exists."})
                return self._send(200, playlists_get())
            if u.path == "/api/playlist/delete":
                gone = [i["id"] for i in playlist_items(pid)]
                playlist_delete(pid)
                uninstall_unsubscribed(gone)
                return self._send(200, playlists_get())
            if u.path == "/api/playlist/duplicate":
                return self._send(200, {"pid": playlist_duplicate(pid, body.get("name"))})
            if u.path == "/api/playlist/activate":
                playlist_activate(pid)
                return self._send(200, playlists_get())
            if u.path == "/api/fav":
                return self._send(200, {"fav": fav_toggle(mid)})
            if u.path == "/api/remove_pc":
                removal_add(mid)
                return self._send(200, removals_get())
            if u.path == "/api/remove_pc/cancel":
                removal_cancel(mid)
                return self._send(200, removals_get())
            if u.path == "/api/game/apply":
                queue_force()
                return self._send(200, {"ok": True})
            if u.path == "/api/play/scenarios":
                return self._send(200, play.scenarios([str(x).upper() for x in body.get("mods") or []]))
            if u.path == "/api/play/save":
                st = play.settings_get()
                for k in ("mods", "scenario", "load_save", "extra_args", "presets"):
                    if k in body:
                        st[k] = body[k]
                play.settings_put(st)
                if party.HOST["on"]:
                    threading.Thread(target=party.host_refresh, daemon=True).start()
                return self._send(200, st)
            if u.path == "/api/party/host":
                return self._send(200, party.host_start())
            if u.path == "/api/party/host/stop":
                return self._send(200, party.host_stop())
            if u.path == "/api/party/host/refresh":
                party.host_refresh()
                return self._send(200, party.host_status())
            if u.path == "/api/party/host/launch":
                return self._send(200, party.host_launch(game_running))
            if u.path == "/api/party/join":
                party.set_game_running(game_running)
                return self._send(200, party.member_join(body.get("host"), body.get("name")))
            if u.path == "/api/party/leave":
                return self._send(200, party.member_leave())
            if u.path == "/api/party/ready":
                return self._send(200, party.member_ready(body.get("ready")))
            if u.path == "/api/party/sync":
                return self._send(200, party.member_sync(body.get("guids")))
            if u.path == "/api/download/playlist":
                pid = int(body.get("pid") or 0) or active_playlist()
                want = {i["id"]: i["name"] or "" for i in playlist_items(pid)}
                for did, name in _closure_from_db(list(want)).items():
                    want.setdefault(did, name)
                inst = installed_ids()
                todo = [{"guid": g, "name": n} for g, n in want.items() if g not in inst]
                if todo:
                    with db() as conn:
                        meta = {r["id"]: (r["size"], r["thumb"]) for r in conn.execute(
                            "SELECT id, size, thumb FROM mods WHERE id IN (%s)" % ",".join("?" * len(todo)), [t["guid"] for t in todo]).fetchall()}
                    for t in todo:
                        t["size"], t["thumb"] = meta.get(t["guid"], (0, ""))
                return self._send(200, wsdl.start(todo))
            if u.path == "/api/update/check":
                return self._send(200, update.check())
            if u.path == "/api/update/apply":
                return self._send(200, update.apply())
            if u.path == "/api/window":
                # remember the app window's size and position; the launcher reopens it the same way
                try:
                    w = {k: int(body[k]) for k in ("w", "h", "x", "y")}
                    w["max"] = bool(body.get("max"))
                    if w["w"] >= 600 and w["h"] >= 400:
                        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "window.json"), "w") as f:
                            json.dump(w, f)
                except Exception:
                    pass
                return self._send(200, {"ok": True})
            if u.path == "/api/update/restart":
                return self._send(200, update.restart())
            if u.path == "/api/mods/check-updates":
                return self._send(200, backend_update_check(force=True))
            if u.path == "/api/downloads/clear":
                del wsdl.HISTORY[:]
                return self._send(200, {"ok": True})
            if u.path == "/api/download/stop":
                return self._send(200, wsdl.stop())
            if u.path == "/api/download/install":
                return self._send(200, wsdl.install())
            if u.path == "/api/party/workshop":
                party.set_game_running(game_running)
                return self._send(200, party.ws_start())
            if u.path == "/api/party/launch":
                return self._send(200, party.member_launch())
            if u.path == "/api/party/prefs":
                pr = party.prefs()
                for k in ("name", "auto_join"):
                    if k in body:
                        pr[k] = body[k]
                party.prefs_put(pr)
                return self._send(200, pr)
            if u.path == "/api/play/launch":
                if party.HOST["on"]:
                    # hosting a party: same as the Party tab's Launch (open to the party, members join)
                    r = party.host_launch(game_running)
                    if r.get("ok"):
                        r["party"] = True
                    return self._send(200, r)
                st = play.settings_get()
                return self._send(200, play.launch([str(x).upper() for x in st["mods"]], st["scenario"], st["load_save"],
                                                   st.get("extra_args") or "", game_running))
            if u.path == "/api/play/build":
                return self._send(200, play.build_start(mid))
            if u.path == "/api/play/unbuild":
                return self._send(200, play.unbuild(mid))
            if u.path == "/api/game/close":
                return self._send(200, close_game())
            if u.path == "/api/game/launch":
                return self._send(200, {"result": launch_game()})
            if u.path == "/api/import_installed":
                return self._send(200, {"added": import_installed()})
            if u.path == "/api/sync":
                started = SYNC.full() if body.get("full") else SYNC.incremental()
                return self._send(200, {"started": started, "sync": SYNC.status})
            if u.path == "/api/sync/stop":
                SYNC.stop_flag = True
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "no such route"})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true", help="full sync then exit")
    ap.add_argument("--port", type=int, default=int(CONFIG.get("port") or 8765))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    init_db()
    if a.sync:
        SYNC.full()
        while SYNC.running:
            s = SYNC.status
            print(f"\r[sync] {s['phase']} page {s['page']}/{s['pages']}  {s['done']}/{s['total']}   ", end="", flush=True)
            time.sleep(1)
        print("\n[sync] done")
        return
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    if n == 0 or not meta_get("full_sync_at"):
        print("[db] building the mod list in the background (a few minutes). The page works while it fills.")
        SYNC.full()
    elif int(CONFIG.get("auto_sync_minutes") or 0) > 0:
        SYNC.incremental()
    threading.Thread(target=auto_sync_loop, daemon=True).start()
    ENRICH.start()
    import_installed()
    threading.Timer(4, download_missing_subscriptions).start()
    party.share_start()
    threading.Thread(target=update.check_loop, daemon=True).start()
    write_queue(2)   # queue.txt reflects the active playlist from the start
    if CONFIG.get("launch_game_on_open"):
        threading.Timer(3, launch_game).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Reforger Mod Browser at {url}   ({n} mods in the database)  Ctrl+C to stop")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
