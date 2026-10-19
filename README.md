# SamRegionHop

**Change your Samsung phone's region (CSC) from your computer. No flashing, no root.**

Unofficial · not affiliated with Samsung · macOS, Windows, Linux

It does the same job as SamFw's "Change CSC", built around one rule: **if anything is uncertain, stop before changing anything.**

> Not affiliated with or endorsed by Samsung. Changing a phone's region can factory reset it. Use it at your own risk on your own phone, and back up first.

## What it does and doesn't do

- **Does:** switches between regions that are already inside the phone's installed firmware, using Samsung's own modem command for this.
- **Doesn't:** flash firmware, touch the bootloader, root the phone, or trip Knox. It can't add regions from a different firmware package or model (e.g. US carrier codes on an international phone).
- **May wipe:** the phone's own setting often says a region change resets it. On the phones tested so far it didn't, but treat a wipe as possible.

## How it decides which regions to offer

For the connected phone, a region is offered only if **both** of these hold:
1. It appears in the phone's own installed region list, and its folder is populated.
2. Samsung's update server confirms that region's firmware for this exact model uses the same package as the phone.

If either can't be confirmed (offline, server refusing, phone not listing its regions), the region isn't offered. Region names (`regions.json`) are only labels, merged from three public CSC lists. Codes marked "sources differ" are disputed.

## Safety model (short version)

- The modem can only be sent 7 allowlisted commands. Everything else is refused before it reaches the phone.
- The modem port must be proven to be the same phone adb sees: model, firmware, and serial from USB or the modem.
- The pending region is read before and after the change. The phone is only rebooted automatically when the change is acknowledged cleanly **and** reads back as the target.
- Once the change command has been sent, the tool never claims "nothing changed". Any doubt ends in a clear "assume the next reboot switches region" message.
- Every run is logged locally (`~/.samregionhop/logs`), with IMEI, phone number and unique number masked.

## Requirements

- Python 3 and `adb` ([Android platform-tools](https://developer.android.com/tools/releases/platform-tools)) on the PATH
- A USB data cable. The region change needs USB; Wi-Fi debugging isn't enough.
- On the phone: Developer options, with **USB debugging** and **Stay awake** on

| | macOS | Windows | Linux |
|---|---|---|---|
| Status | ✅ Tested on real hardware | ⚠️ Untested | ⚠️ Untested |
| Modem port | `/dev/cu.*`, no driver needed | `COM*`, needs [Samsung's USB driver for mobile phones](https://developer.samsung.com/android-usb-driver) | `/dev/ttyACM*`, no driver needed |
| Extra steps | none | none | stop ModemManager (`sudo systemctl stop ModemManager`) and add yourself to the `dialout` group |

"Untested" means the same code and safety checks, but nobody has run it there on a real phone yet. If something differs, it stops before changing anything; please send a compatibility report.

## Setup

macOS / Linux:
```sh
python3 -m venv .venv && .venv/bin/pip install pyserial
./ui          # opens the step-by-step page in your browser
./csc --help  # command-line version
```

Windows (Command Prompt):
```bat
py -m venv .venv && .venv\Scripts\pip install pyserial
ui.bat
csc.bat --help
```

## Using it

1. **Read phone.** Shows model, firmware and current region, then finds the regions this phone can reliably switch to.
2. **Check updates.** Asks Samsung which regions have an update path from your build.
3. **Test modem.** Dial `*#0*#` first and leave that screen open. Sends only read-only queries.
4. **Dry run.** Runs every check of a real change, then stops.
5. **Change region.** Tick the checklist, then type the region code to confirm.

Quit Smart Switch before steps 3–5: it takes over the phone's USB connection and hides the modem port.

## Compatibility

| Model | Chip | One UI (Android) | Computer | Change | Result | Data wiped? |
|---|---|---|---|---|---|---|
| SM-S928B (Galaxy S24 Ultra) | Snapdragon (SM8650) | 7.0 (15) | macOS | INS → EUX | ✅ Worked | No |
| SM-S928B (Galaxy S24 Ultra) | Snapdragon (SM8650) | 9.0 (17) | macOS | EUX → INS | ✅ Worked | Not reported |

Every other model is untested. Different chips or One UI versions may answer the modem in formats the tool doesn't recognise yet. If so, it stops before changing anything.

### Help add your phone

After any run, especially one that stopped, click **Make compatibility report** (or run `./csc report`). It saves a Markdown report in `~/.samregionhop/reports` with your serial, IMEI and phone number masked. Read it, then attach it to a new GitHub issue. Each report lets the next version support another phone.

## Development

```sh
.venv/bin/python -m unittest discover -s tests   # macOS / Linux
```

The phone-simulation tests need POSIX pseudo-terminals, so they're skipped on Windows. The tests drive the real code against a simulated phone (a fake adb, and a fake modem on a pseudo-terminal), using replies captured from a real S24 Ultra and data from Samsung's update server.

## License

Copyright (C) 2026 Arjun Bhagavatula

SamRegionHop is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but **without any warranty**; see [LICENSE](LICENSE) for details.
