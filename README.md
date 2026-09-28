# Reforger Mod Browser

A Steam-style launcher and Workshop browser for **Arma Reforger**, built for playing modded
co-op with friends.

- **Browse** all 49,000+ Workshop mods: search, categories, tags, sort by popular / rated /
  newest / updated / trending, custom date ranges.
- **Playlists** and one-click installs, with dependencies resolved automatically.
- **Play tab**: tick mods, pick a scenario, launch straight into it. Local Workbench projects
  can be packed and loaded next to Workshop mods.
- **Party**: host a game and friends sync your exact mod list (Workshop mods download on their
  side, your own local mods copy straight from your PC), then join the moment you are in game.
- **Auto-updates** installed mods, and the app itself updates from the host's copy.
- Themes: original Xbox, Xbox 360 (with dashboard sounds), Steam.

## Run it

Windows only. Download the repository (Code > Download ZIP), unzip anywhere and double-click
`Reforger Mod Browser.exe`. It carries its own Python and opens as its own window using the
Edge that ships with Windows. Nothing to install.

First start downloads the Workshop list (2 to 3 minutes); the page works while it fills.
`console.exe` starts the same thing with a console window. Problems are written to `app.log`.

## Files

| File | What it is |
|---|---|
| `server.py` | Local web server, Workshop sync, database, API |
| `launcher.py` | Starts the server and the app window |
| `play.py` | Play tab: mod scanning, dependency order, launching, packing local projects |
| `party.py` | Party hosting / joining and mod sync between PCs |
| `wsdl.py` | Workshop downloads through the game's server tool |
| `update.py` | App self-update from the party host |
| `ui.html` | The whole interface |
| `sounds/` | Xbox / Xbox 360 dashboard sounds |

The bundled Python runtime is the official embeddable Python 3.12 (see `LICENSE.txt`).
