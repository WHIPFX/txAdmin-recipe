# WHIP txAdmin recipe

WHIP is a complete, open-source framework for FiveM servers. Players make a character, carry and stash items, keep
money in the bank, buy, fuel and tune cars, own and furnish a home, and get hurt and revived. Admins run the server
from inside the game: `/whip` opens a panel for players, items, settings, banks, garages, shops and homes.

This repository holds the txAdmin recipes that install WHIP. The framework itself lives in the
[resource repositories](#whats-included). The recipes use txAdmin's
[recipe format](https://github.com/citizenfx/txAdmin/blob/master/docs/recipe.md).

## Requirements

- A FiveM server with txAdmin, on either game:
  - **FiveM Legacy**: FXServer, artifact 38435 or newer
  - **GTA V Enhanced**: cfx-server
- MariaDB 10.2.3+ or MySQL 8.0.16+, and a database user that can create databases.
- About 1 GB of free disk space.
- At least 10 Mbit/s to GitHub. txAdmin gives each download 150 seconds, and the largest is about 145 MB.
- The voice ports open inbound, like port 30120: **UDP 7882-7893** and **TCP 7881**. WHIP runs its own voice server
  on the same machine and sets it up on the first start.

WHIP installs as a **new server**. The recipe builds the whole server (resources, database and `server.cfg`), so it
can't be added to a server you already have.

## Install

1. Start the server and open txAdmin. On the setup page, choose **Remote URL Template** and paste the recipe for your
   game:

   | Game | Recipe URL |
   | --- | --- |
   | FiveM Legacy | `https://raw.githubusercontent.com/WHIPFX/txAdmin-recipe/main/whip-legacy.yaml` |
   | GTA V Enhanced | `https://raw.githubusercontent.com/WHIPFX/txAdmin-recipe/main/whip-enhanced.yaml` |

2. Enter your server license key (from [portal.cfx.re](https://portal.cfx.re)) and your database login. txAdmin
   downloads WHIP and oxmysql, creates an empty database, turns on OneSync, writes `server.cfg` and makes your txAdmin
   account an admin. There's no SQL file to import: WHIP creates its own tables.
3. Start the server. On Windows, the first start asks whether to let `livekit-server.exe` through the firewall: click
   **Allow**. You can also run `resources/whip_voice/service/Open voice ports.bat`.

### First run

1. A few seconds after `Framework loaded successfully!`, the console prints a **WHIP setup** report. Anything marked
   **FIX** tells you what to change. Type `whipsetup` in the console to see the report again.
2. Join the server. Your Cfx.re account needs a linked Steam account: the recipe turns on `sv_enforceSteamAuth`, and
   FiveM asks players without one to link it.
3. Make a character. The appearance editor opens.
4. Press **F2** for the inventory. `/w` opens the quick tools and `/whip` opens the admin panel.

To make someone else an admin, ask them to type `/w`. The console then prints the `add_ace` line to add for them.

The full documentation is in `resources/whip_core/docs.html`. Open it in a browser; it works offline.

## Updating

Each resource has its own releases on GitHub. To update one:

1. Read the release's notes on the resource's releases page.
2. Stop the server.
3. Replace the resource's folder with the new release (**Source code (zip)** on its releases page).
4. Start the server. Your database carries over: whip_core updates its tables on the next start.

Before you replace a folder, copy out anything the server saved in it:

| Saved in | What it is |
| --- | --- |
| `whip_core/pack_exports` | Director packs you exported |
| `whip_media_props/img/props`, `whip_media_previews/img/vehicles` | Pictures taken in game |
| `whip_autos/engines.json` | The engine list, if you ran `/whiplashengines` |

## What's included

In `resources`, each started by its own `ensure` line in `server.cfg`:

| Resource | What it is |
| --- | --- |
| [whip_core](https://github.com/WHIPFX/whip_core) | Players, characters, inventory, money and banks, jobs, vehicles, garages, properties, the UI, the Director and the admin panel |
| [whip_gunny](https://github.com/WHIPFX/whip_gunny) | Weapons: magazines, calibers, melee, throwables and attachments |
| [whip_fuel](https://github.com/WHIPFX/whip_fuel) | Fuel tanks, pumps with a hose, and the jerry can |
| [whip_rope](https://github.com/WHIPFX/whip_rope) | A physics rope to carry, tie and tow with |
| [whip_phone](https://github.com/WHIPFX/whip_phone) | The Duo phone, with apps as modules |
| [whip_autos](https://github.com/WHIPFX/whip_autos), [whip_autos_shop](https://github.com/WHIPFX/whip_autos_shop) | Whiplash: customization, turbos and nitrous, and the shop in Burton |
| [whip_voice](https://github.com/WHIPFX/whip_voice) | 3D voice chat, on a voice server it runs itself |
| [whip_chat](https://github.com/WHIPFX/whip_chat), [whip_loadingscreen](https://github.com/WHIPFX/whip_loadingscreen) | Chat and the loading screen |

In `resources/[assets]/[media]`, the media packs. They're kept out of whip_core so its updates stay small:

| Resource | What it is |
| --- | --- |
| [whip_media_clothing_male](https://github.com/WHIPFX/whip_media_clothing_male), [whip_media_clothing_female](https://github.com/WHIPFX/whip_media_clothing_female) | Clothing pictures for the character editor |
| [whip_media_props](https://github.com/WHIPFX/whip_media_props) | Pictures for the furniture catalog |
| [whip_media_previews](https://github.com/WHIPFX/whip_media_previews) | Vehicle, NPC and tattoo pictures, and the emote menu's preview videos |
| [whip_media_map](https://github.com/WHIPFX/whip_media_map) | The pause menu's 3D map, GTA's blip icons and the radar's map tiles |

In `resources/[assets]`, the map packs and Chameleon Paint:

| Resource | What it is |
| --- | --- |
| [whip_homes_mirrorpark](https://github.com/WHIPFX/whip_homes_mirrorpark) | Every Mirror Park house as a walk-in home, plus 25 garages |
| [whip_graphics](https://github.com/WHIPFX/whip_graphics) | LED streetlights, and WHIP's look for the city's lighting and sky |
| [chameleon_paint](https://github.com/WHIPFX/chameleon_paint) | WildBrick142's Chameleon Paint: the colour ramps whip_autos' Chameleon paints need. It's the author's work under their own terms, not part of WHIP, and must never be sold |

`ensure [assets]` starts everything in `[assets]`.

In `resources/[ox]`, [oxmysql](https://github.com/WHIPFX/oxmysql), which WHIP uses for its database. It's WHIPFX's
fork of oxmysql with one fix: a query fails straight away when the database is down, instead of waiting forever.

### FiveM Legacy and GTA V Enhanced

Every resource runs on both games. Resources with models ship them twice: `stream/` for Legacy and
`stream_enhanced/` for Enhanced. The two recipes differ only in `server.cfg`. The Enhanced recipe sets
`setr whip_platform enhanced` and gives slow connections more time to download. The Legacy recipe sets the game build
with `sv_enforceGameBuild`.

## License

WHIP is licensed under the GNU LGPL 3.0: see each repository's `COPYING.LESSER` and `NOTICE.md`. These recipes are
under the same licence (`COPYING.LESSER`, with the GPL 3.0 it builds on in `COPYING`).
