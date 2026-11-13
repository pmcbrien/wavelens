# Wavelens Scanner — the admin page

Your Wavelens Scanner (the Raspberry Pi) has a small web page for looking after it from your phone.
Nothing on it is needed day to day; it is there for Wi-Fi, updates, settings and the occasional look at
the logs.

## Opening it

On a phone or laptop **on the same Wi-Fi as the Pi**, open:

    http://wavelens.local:8080

If that name doesn't resolve (some Android phones can't look up `.local` names), use the Pi's IP
address instead, `http://192.168.x.x:8080`:

- the Wavelens app's **Companion** screen shows the Pi's address in the hardware line when the Pi reports one, or
- plug a monitor and keyboard into the Pi and run `hostname -I`, or
- look at your router's device list for "wavelens".

If you renamed the Pi on the Wi-Fi page, use `http://<new-name>.local:8080`.

## First visit: set a password

The first time the page is opened it asks you to choose an admin password (at least 8 characters). Every
later visit asks for that password; a login lasts 12 hours on that device. Change it on the **Account** page.

## The pages

| Page | What it does |
|------|--------------|
| **Status** | If something is wrong, a red box at the top says what in plain words and has the fix as a button (below). **Connected devices**: a live diagram (every 5 s) of the Pi with everything around it — the power supply (input voltage, the supply's current limit, how much the USB devices ask for), the phone over Bluetooth, Wi-Fi / Ethernet (network, signal, IP, MAC), and each USB device (RTL-SDR, HackRF, Spotter, GPS…) with its job, serial, USB port and bus/device address, speed and power. Green works, amber needs a look, red is a problem, dashed is not connected. Below it, the USB table and every voltage and current the Pi reports (a Pi 5 reports each rail). **Checks**: anything the diagnostics found that needs a look. Then the bridge and the Pi's facts, recent events from the bridge's log, and buttons to restart the bridge, reboot, run diagnostics or download a support report. |
| **Diagnostics** | A test of the whole unit that changes nothing: power (under-voltage, the USB power budget), temperature and fan, CPU, memory, storage, SD card errors, out-of-memory kills, the radios on USB against what the scanner opened, USB dropouts, the kernel TV driver, Bluetooth, Wi-Fi signal, whether GitHub can be reached for updates, the clock, the area, digital voice and the software version. Worst first, each with what to do. Tiles for CPU, memory, temperature, storage, power and the system, the busiest programs, and this boot's kernel events. *Download support report* saves all of it with the last 400 bridge lines (no passwords or Wi-Fi keys). |
| **Logs** | The last 300 lines of the bridge's log, refreshing every 2 seconds. The very chatty signal lines are hidden unless you tick *include rf/spec lines*. *Download full log* saves the last 5000 lines to send to support. |
| **Settings** | The radio name the app lists under *Find*, and the bridge's options from `/etc/default/wavelens-bridge`, with the file's own explanations below. Saving restarts the bridge. The **Bluetooth and remote access** card has four on/off switches: Wi-Fi set-up from the app (`WAVELENS_WIFI_SETUP`), updates and area from the app (`WAVELENS_APP_CONTROL`), accepting Bluetooth pairing without a prompt (the `wavelens-bt-agent` service) and SSH login (the `ssh` service). Only what changed is touched; the bridge restarts only for the first two. |
| **Area** | Where the Scanner is used: a two-letter country and a postcode, any country. *Set area and download* fetches the public licence records around that place (where a country publishes them, today the United States) so hits are named by licensee. Needs Wi-Fi; restarts the bridge. **Your own RadioReference exports**: upload the CSV downloads from your RadioReference account here (a county's, a state's or the nationwide frequencies, a trunked system's sites, its talkgroups; several at once), see what each was recognised as, and delete them. The files stay on the Scanner in `/var/lib/wavelens/area/rr/`; uploading or deleting restarts the bridge. The table shows the size of the FCC database on the Pi and how many records it holds. **Follow me** (`wavelens-area follow on`): when the phone shares its position with the Scanner and you have left the middle of the area (40 % of the radius, at least 15 km), the Scanner re-centres the area on you and swaps in the local licences without restarting, at most once every 10 minutes. It needs an internet connection at that moment; without one it keeps the area it has and tries again. |
| **Frequencies** | Your own frequency list, kept on the Scanner. Add, edit, group, search and delete channels; lock one out so Follow never stops on it; import a list you already keep and export yours; put back an earlier version. See [Your own frequency list](#your-own-frequency-list) below. No restart: a change is in use within a few seconds. |
| **Update** | Installed and available version, *Check now*, *Install update*, *Roll back to previous*, and a box for installing updates automatically overnight. An install shows each step as it happens (check GitHub, save the working version, install, check it loads, Spotter firmware, restart, confirm it runs) with the time taken, and a clear result at the end, including when a version that didn't start was put back. The page keeps following through the restart near the end (you stay logged in). *History* lists the last ten results; the full log is one tap away. Updates are free for life and come from the public GitHub repo `pmcbrien/wavelens`; the Pi needs Wi-Fi to fetch them. |
| **Wi-Fi** | Networks nearby, join one, forget saved ones, and rename the Pi. Do this on your home Wi-Fi, not public Wi-Fi: the password you type travels over the local network unencrypted. |
| **Spotter** | *Spotters on this Pi*: each ESP32 plugged into the Pi's USB, whether it's working, what it's heard, and its job (Automatic, Bluetooth only, a Wi-Fi channel, …) — change it from the list. Then flash a Spotter plugged into the Pi's USB: download the latest firmware (one image per chip: classic ESP32, S3, C3, C5, C6), or upload a `.bin`, pick the port, flash. With the downloaded firmware the Pi asks the board which chip it is and flashes the matching image; an uploaded file is flashed as given (`--chip auto`). Progress streams onto the page. Needs `esptool` on the Pi (`sudo apt install esptool`). |
| **Power** | Restart the bridge, restart the admin page, reboot, shut down. Each asks you to confirm. |
| **Account** | Change the admin password, log out. |

## Your own frequency list

The **Frequencies** page is where you keep the channels you care about. What the Scanner does with them:

- **Names.** A hit on one of your frequencies is named from your list first, ahead of any licence record or RadioReference export.
  A hit on a channel's repeater input says *keying up nearby*.
- **Mode.** Listen and Follow tune the channel in the mode you set (FM, AM, DMR, P25, NXDN) instead of working it out.
- **Lockout.** Follow never stops on a channel you lock out: a pager, a busy repeater, a data channel. This adds to the
  lockout list in the app; neither replaces the other.

Each channel has a name, a frequency, and optionally a repeater input, mode, tone or code, group, who uses it, tags, notes,
an emission designator, a bandwidth and a position. Two channels are the same channel when frequency and tone agree, and the
page won't keep two of those.

**Import** reads a file from your phone or computer, works out what kind it is from its contents, and shows what it read
(and which rows it left out, and why) before anything is added:

| File | What is read |
|------|--------------|
| Wavelens frequency list (JSON) | everything, as exported |
| Plain CSV | a header row with a frequency column (`frequency`, `freq`, `mhz`, `frequency_mhz` or `frequency_hz`); `name`, `input`, `mode`, `tone`, `group`, `service`, `tags`, `lockout`, `notes`, `lat`, `lon` when present |
| CHIRP CSV | name, frequency, duplex and offset, tone or DCS, mode, skip, comment |
| Gqrx bookmarks | frequency, name, modulation, bandwidth; the first tag becomes the group |
| SDR++ frequency manager | an exported list or the whole file: name, frequency, bandwidth, mode |
| RadioReference CSV | your own export of conventional frequencies. Trunked sites and talkgroups go in the area folder instead (the **Area** page) |

**Export** writes the whole list or one group as the Wavelens JSON, plain CSV, CHIRP CSV, Gqrx bookmarks or an SDR++ list.
The Wavelens file keeps everything; the others carry what they have room for.

**Backups.** Every change keeps the list it replaced, the last 20 of them, in `/var/lib/wavelens/frequency-backups/`.
*Put back* restores one, and keeps today's list as a backup too. For a copy that survives a lost SD card, export the list.

**The format.** The list is one JSON file, `/etc/wavelens/frequencies.json` (updates never touch it), format
`wavelens-frequencies/1`. Its layout is published as a JSON Schema (the link on the page, or `freqdb.py schema`).
Frequencies are whole hertz; emission designators are the ITU ones (11K2F3E); CTCSS tones are in hertz, DCS codes octal,
P25 NACs hex; times are ISO 8601 in UTC; positions are WGS 84 decimal degrees.

Over SSH the same list has a command line:

    sudo python3 /usr/local/lib/wavelens/freqdb.py list
    sudo python3 /usr/local/lib/wavelens/freqdb.py check my-list.csv        # what an import would take; changes nothing
    sudo python3 /usr/local/lib/wavelens/freqdb.py import my-list.csv --mode add
    sudo python3 /usr/local/lib/wavelens/freqdb.py export --format chirp > radio.csv

## When something goes wrong

The Status page checks the scanner software every time you open it. If it isn't running, or keeps stopping
and restarting, a red box says what's wrong and offers the fix:

| What the box says | What to do |
|---|---|
| *This version of the scanner software doesn't run on this Pi.* | **Put the previous version back**, or **Install the latest update** if there's no previous version. |
| *Some of the scanner software's files are missing.* | **Install the latest update** — it reinstalls everything and checks the result. |
| *A radio dongle isn't answering.* | Unplug each USB radio, wait five seconds, plug it back in. It's picked up within ten seconds. |
| *The Pi's Bluetooth isn't working.* | **Reboot**. If it's the same afterwards, power the Pi off for ten seconds. |
| *The scanner software keeps stopping and restarting.* | **Restart the scanner**; if it stops again, **Put the previous version back**. |

Every box also has **Download log for support** — email it to wavelensapp@gmail.com.

Updates look after themselves: if a new version doesn't start, the Pi puts the one that worked back on its
own and the Status page says so ("Last update: … was put back"). Nothing else needs doing — the next update
that fixes it installs normally. Details: [UPDATES.md](../UPDATES.md#an-update-cant-leave-the-scanner-dead).

**Can't open the page at all?** The Pi isn't on your Wi-Fi, or is off. Check it has power (the red LED),
give it two minutes after plugging in, and try its IP address instead of `wavelens.local`. If it was never
joined to your Wi-Fi, join it from the app (next section), or plug in a screen and keyboard and run `sudo nmtui`.

## Joining Wi-Fi from the app

A Scanner that isn't on any network yet can't show this page, so the app can put it on one over Bluetooth.
Connect to the Scanner in the Wavelens app, open **Companion** (Android) or **Setup** (iPhone / iPad), and under
*Wi-Fi on the Pi* tap **Join Wi-Fi**. The Pi lists the networks it can see; pick yours, type the password, tap
**Join**. It takes up to a minute, then the app shows the network and the Pi's address, which is where this
admin page is. *Other network…* is for a network that hides its name.

- The Pi does the scanning and joining. The phone only passes on your choice and the password over the
  Bluetooth link, and keeps neither. The app still has no internet access of its own.
- The password is not written to the Pi's log or sent back. NetworkManager stores it on the Pi, as it does
  for any saved network.
- If the Pi was on another network it leaves that one. After a wrong password it normally goes back to the network it was on.
- Forgetting a saved network and renaming the Pi stay on this page's **Wi-Fi** tab.
- The Bluetooth link to a Scanner is not paired or encrypted, so do this at home, not in a public place.
  Anyone in Bluetooth range who connects to the Pi could use it too; to switch it off once the Pi is set up,
  turn off **Wi-Fi set-up from the app** on the **Settings** page (*Bluetooth and remote access*).
- Needs the Pi's software at 2026.11.10.8 or later and NetworkManager (Raspberry Pi OS Bookworm and later).

## Forgot the password?

Either of these makes the page ask for a new password on the next visit:

- over SSH: `sudo wavelens-admin --reset-password`
- or power the Pi off, put the SD card in a computer and delete `/etc/wavelens/admin.json`

## How it is secured

- **LAN only.** The page listens on the Pi's own network address. It is not reachable from the
  internet unless you deliberately forward a port on your router to it — don't.
- **Plain HTTP.** There is no certificate, so traffic between your phone and the Pi is not encrypted.
  On your home Wi-Fi that is fine; on a public or shared network, don't type passwords into it.
- **One password**, stored only as a salted PBKDF2 hash in `/etc/wavelens/admin.json`. Five wrong guesses
  in ten minutes from one address blocks logins from it for ten minutes. Every form carries a one-time
  token so another website open in your browser can't submit actions to the Pi.
- **Nothing leaves the Pi.** The scanner is receive-only. The only outbound connection the Pi ever makes
  is to GitHub, to check for or fetch updates and Spotter firmware, and only when you ask (or when you've
  turned on overnight updates).

## Installing

`install-service.sh` (run once from the `companion/pi` folder, `sudo ./install-service.sh`) installs the admin
page alongside the bridge: `wavelens_admin.py` into `/usr/local/lib/wavelens`, the `wavelens-admin` command
into `/usr/local/bin`, and the `wavelens-admin` systemd service, started at boot. The port is 8080; set
`WAVELENS_ADMIN_PORT` in `/etc/default/wavelens-bridge` to change it. Logs: `journalctl -u wavelens-admin -f`.

## The Pi's name and Bluetooth pairing

`provision.sh` names the Pi `wavelens` (or `WAVELENS_HOSTNAME`), so this page is at `http://wavelens.local:8080`.
`install-service.sh` run by hand does the same for a Pi still on the factory name `raspberrypi` and keeps a
name somebody chose; `WAVELENS_HOSTNAME=name` picks one and `WAVELENS_HOSTNAME=keep` never renames. The
**Wi-Fi** tab can rename it later.

The installer also enables `wavelens-bt-agent`, which accepts a phone's or computer's Bluetooth pairing
request without a prompt, because the Pi has no screen to confirm on. Anyone in Bluetooth range can pair; the
bridge already accepts connections from any phone running the app. Off: `sudo systemctl disable --now
wavelens-bt-agent`, or install with `WAVELENS_BT_AUTOPAIR=0`.
