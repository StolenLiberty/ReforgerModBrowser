Reforger Mod Browser
====================

Double-click "Reforger Mod Browser.exe" (or the desktop shortcut it creates
on first run). It opens as its own window. Close the window to stop it.
Nothing to install: it carries its own copy of Python and uses the Edge
that ships with Windows for the window.

First run downloads the whole workshop list (49,000+ mods, 2 to 3 minutes).
The page works while it fills. After that it fetches download counts for
every mod in the background and re-checks for new and updated mods every
30 minutes while it is open.

If something goes wrong, app.log in this folder says what. console.exe
starts the same thing with a console window so you can watch it.

Playlists: the active one is written to
   Documents\My Games\ArmaReforger\profile\BetterWorkshop\queue.json
for the companion in-game mod (not built yet).

Settings: config.json.   Data: mods.db (delete it to start over).
