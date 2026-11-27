# WHIP txAdmin recipe

WHIP is a complete, open-source framework for FiveM servers. Players make a character, carry and stash items, keep
money in the bank, buy, fuel and tune cars, own and furnish a home, and get hurt and revived. Admins run the server
from inside the game: `/whip` opens a panel for players, items, settings, banks, garages, shops and homes.

This repository holds the txAdmin recipes that install WHIP. The framework itself lives in the
[resource repositories](#whats-included).

## Requirements

- A FiveM server with txAdmin, on either game:
  - **FiveM Legacy**: FXServer, artifact 38435 or newer
  - **GTA V Enhanced**: cfx-server
- MariaDB 10.2.3+ or MySQL 8.0.16+, and a database user that can create databases
- About 1 GB of free disk space, and at least 3 Mbit/s to GitHub
- The voice ports open inbound, like port 30120: **UDP 7882-7893** and **TCP 7881**

WHIP installs as a **new server**: it can't be added to a server you already have.

## Install

1. Start the server and open txAdmin. On the setup page, choose **Remote URL Template** and paste the recipe for your
   game:

   | Game | Recipe URL |
   | --- | --- |
   | FiveM Legacy | `https://raw.githubusercontent.com/WHIPFX/txAdmin-recipe/main/whip-legacy.yaml` |
   | GTA V Enhanced | `https://raw.githubusercontent.com/WHIPFX/txAdmin-recipe/main/whip-enhanced.yaml` |

2. Enter your server license key (from [portal.cfx.re](https://portal.cfx.re)) and your database login. txAdmin
   installs WHIP, creates the database, writes `server.cfg` and makes your txAdmin account an admin. There's no SQL
   file to import.
3. Start the server. On Windows, allow `livekit-server.exe` through the firewall when asked: it's WHIP's voice server.

WHIP's pictures and videos load from WHIP's CDN, so they aren't part of the install. To keep them on your own server,
see [Pictures on your server](#pictures-on-your-server).

### If the install fails

Delete the half-made server folder first: txAdmin only installs into an empty folder. Then:

- **A download timed out**: the connection to GitHub is too slow. Try again.
- **An unzip timed out**: a slow disk, or antivirus scanning each new file. Pause the antivirus and try again.

## First run

1. The console prints a **WHIP setup** report. Change anything marked **FIX**; type `whipsetup` to see it again.
2. Join the server. Your Cfx.re account needs a linked Steam account.
3. Make a character. Press **F2** for the inventory; `/w` opens the quick tools and `/whip` the admin panel.

To make someone else an admin, ask them to type `/w`: the console prints the `add_ace` line to add for them.

The full documentation is `resources/whip_core/docs.html`. Open it in a browser; it works offline.

## Pictures on your server

The clothing, furniture, vehicle, NPC and tattoo pictures and the emote videos load from WHIP's CDN
(`media.whipfx.net`) when a screen shows them. To keep them on your server instead, stop it and run the script the
recipe put in the server folder:

- Windows: `powershell -ExecutionPolicy Bypass -File get-whip-media.ps1`
- Linux: `sh get-whip-media.sh` (it needs `curl` and `unzip`)

If it stops, run it again: it keeps what it already has. On the next start, the installed packs serve their own
pictures.

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
| `whip_autos/engines.json` | The engine list, if you ran `/whiplashengines` |

## What's included

In `resources`:

| Resource | What it is |
| --- | --- |
| [whip_core](https://github.com/WHIPFX/whip_core) | Players, characters, inventory, money and banks, jobs, vehicles, garages, properties, the UI, the Director and the admin panel |
| [whip_gunny](https://github.com/WHIPFX/whip_gunny) | Weapons: magazines, calibers, melee, throwables and attachments |
| [whip_fuel](https://github.com/WHIPFX/whip_fuel) | Fuel tanks, pumps with a hose, and the jerry can |
| [whip_rope](https://github.com/WHIPFX/whip_rope) | A physics rope to carry, tie and tow with |
| [whip_phone](https://github.com/WHIPFX/whip_phone) | The Duo phone, with apps as modules |
| [whip_dealership](https://github.com/WHIPFX/whip_dealership) | Simon's car lot: buy cars with cash or from the bank, and manage your car keys |
| [whip_autos](https://github.com/WHIPFX/whip_autos), [whip_autos_shop](https://github.com/WHIPFX/whip_autos_shop) | Whiplash: customization, turbos and nitrous, and the shop in Burton |
| [whip_voice](https://github.com/WHIPFX/whip_voice) | 3D voice chat, on a voice server it runs itself |
| [whip_chat](https://github.com/WHIPFX/whip_chat), [whip_loadingscreen](https://github.com/WHIPFX/whip_loadingscreen) | Chat and the loading screen |

In `resources/[assets]`:

| Resource | What it is |
| --- | --- |
| [whip_media_map](https://github.com/WHIPFX/whip_media_map) (in `[media]`) | The pause menu's 3D map, GTA's blip icons and the radar's map tiles |
| [whip_homes_mirrorpark](https://github.com/WHIPFX/whip_homes_mirrorpark) | Every Mirror Park house as a walk-in home, plus 25 garages |
| [whip_graphics](https://github.com/WHIPFX/whip_graphics) | LED streetlights, and WHIP's look for the city's lighting and sky |
| [chameleon_paint](https://github.com/WHIPFX/chameleon_paint) | WildBrick142's Chameleon Paint: the colour ramps whip_autos' Chameleon paints need. It's the author's work under their own terms, not part of WHIP, and must never be sold |
| [bob74_ipl](https://github.com/Bob74/bob74_ipl) | Bob74's IPL loader: it fills the holes in GTA's map and loads GTA Online's interiors. It's the author's work under the MIT licence, not part of WHIP |

From WHIP's CDN, or in `resources/[assets]/[media]` if you [install them](#pictures-on-your-server):

| Resource | What it is |
| --- | --- |
| [whip_media_clothing_male](https://github.com/WHIPFX/whip_media_clothing_male), [whip_media_clothing_female](https://github.com/WHIPFX/whip_media_clothing_female) | Clothing pictures for the character editor |
| [whip_media_props](https://github.com/WHIPFX/whip_media_props) | Pictures for the furniture catalog |
| [whip_media_previews](https://github.com/WHIPFX/whip_media_previews) | Vehicle, NPC and tattoo pictures, and the emote menu's preview videos |

In `resources/[ox]`, [oxmysql](https://github.com/WHIPFX/oxmysql), WHIP's database library: WHIPFX's fork, where a
query fails straight away when the database is down instead of waiting forever.

Every resource runs on both games. The two recipes differ in `server.cfg`, and in the models they download: each
gets only its own game's.

## License

WHIP is licensed under the GNU LGPL 3.0: see each repository's `COPYING.LESSER` and `NOTICE.md`. These recipes are
under the same licence (`COPYING.LESSER`, with the GPL 3.0 it builds on in `COPYING`).
