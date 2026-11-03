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
| **Status** | Bridge running or not, phone connected or not, the radios it found (and anything missing), the Pi's name, IP, Wi-Fi, temperature, disk and uptime. A banner appears when an update is available. Buttons to restart the bridge or reboot. |
| **Logs** | The last 300 lines of the bridge's log, refreshing every 2 seconds. The very chatty signal lines are hidden unless you tick *include rf/spec lines*. *Download full log* saves the last 5000 lines to send to support. |
| **Settings** | The radio name the app lists under *Find*, and the bridge's options from `/etc/default/wavelens-bridge`, with the file's own explanations below. Saving restarts the bridge. |
| **Update** | Installed and available version, *Check now*, *Install update*, *Roll back to previous*, and a box for installing updates automatically overnight. Progress streams onto the page. Updates are free for life and come from the public GitHub repo `pmcbrien/wavelens`; the Pi needs Wi-Fi to fetch them. |
| **Wi-Fi** | Networks nearby, join one, forget saved ones, and rename the Pi. Do this on your home Wi-Fi, not public Wi-Fi: the password you type travels over the local network unencrypted. |
| **Spotter** | Flash a Wavelens Spotter (ESP32) plugged into the Pi's USB: download the latest firmware, or upload a `.bin`, pick the port, flash. Progress streams onto the page. Needs `esptool` on the Pi (`sudo apt install esptool`). |
| **Power** | Restart the bridge, restart the admin page, reboot, shut down. Each asks you to confirm. |
| **Account** | Change the admin password, log out. |

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
