# Installing Bonsai (AppImage)

`Bonsai-x86_64.AppImage` is the whole app in one file: Bonsai, its own
Python and Qt. Nothing else needs installing, and no root access is needed.

Your boards are not inside it. Bonsai keeps them in
`~/.local/share/bonsai-kanban/kanban.db`, so on a new machine (or user
account) it starts with one empty board: To Do, In Progress, Done.

## Requirements

- 64-bit Intel/AMD Linux (x86_64)
- glibc 2.34 or newer: Ubuntu 22.04+, Debian 12+, Fedora 35+, RHEL/Rocky/Alma 9+,
  Linux Mint 21+, openSUSE Tumbleweed. (Check with `ldd --version`.)
- FUSE 2, which AppImages use to run. Most desktops have it; if not:
  - Ubuntu 22.04 / Mint 21 / Debian: `sudo apt install libfuse2`
  - Ubuntu 24.04+: `sudo apt install libfuse2t64`
  - Fedora: `sudo dnf install fuse fuse-libs`
- A few desktop libraries Qt needs. Usually present already; if Bonsai won't
  start, install these and try again:
  - Ubuntu / Debian / Mint: `sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libfontconfig1`
  - Fedora: `sudo dnf install xcb-util-cursor libxkbcommon-x11 mesa-libEGL mesa-libGL fontconfig`

## Install

1. Copy `Bonsai-x86_64.AppImage` to the other machine and put it somewhere
   it will stay, for example `~/Applications`:

   ```sh
   mkdir -p ~/Applications
   mv Bonsai-x86_64.AppImage ~/Applications/
   chmod +x ~/Applications/Bonsai-x86_64.AppImage
   ```

2. Add it to the application menu:

   ```sh
   ~/Applications/Bonsai-x86_64.AppImage --install
   ```

   Bonsai now appears in the application menu under **Office**. (On some
   desktops it shows up after logging out and back in.)

You can also just run the file (double-click it, or
`~/Applications/Bonsai-x86_64.AppImage`) without installing.

If you move the AppImage later, run `--install` again from its new place:
the menu entry points at wherever the file was.

## Update

Replace `~/Applications/Bonsai-x86_64.AppImage` with the new one (keep the
same name and place). Your boards are kept.

## Uninstall

```sh
~/Applications/Bonsai-x86_64.AppImage --uninstall
rm ~/Applications/Bonsai-x86_64.AppImage
```

That removes the menu entry and the app, but leaves your boards. To delete
those too:

```sh
rm -r ~/.local/share/bonsai-kanban
```

## Troubleshooting

- **"Cannot mount AppImage, please check your FUSE setup"**: install FUSE 2
  (see Requirements), or run it without FUSE:
  `~/Applications/Bonsai-x86_64.AppImage --appimage-extract-and-run`
- **Nothing happens / it closes straight away**: run it from a terminal to see
  the error. "Could not load the Qt platform plugin xcb" means one of the Qt
  libraries listed under Requirements is missing.
- **"GLIBC_2.34 not found"**: the system is too old for this build.

## Building the AppImage (developers)

From the project folder, with Python 3 and internet access:

```sh
./build-appimage.sh
```

This writes `install/Bonsai-x86_64.AppImage`. The recipe (desktop entry,
icon, and the `--install`/`--uninstall` entry point) is in `packaging/appimage/`.
