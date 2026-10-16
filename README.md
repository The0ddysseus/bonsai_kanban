# Bonsai - kanban for Linux

<img src="assets/bonsai.svg" width="96" alt="Bonsai's icon: a pixel-art bonsai tree">

A native Linux desktop kanban board, built with PySide6 (Qt) and SQLite.
Runs on any Linux desktop (GNOME, KDE, Cinnamon, XFCE, ...) with Python
3.10 or newer; each user account gets its own board.
Archiving is still to come.

## What works right now

- **Multiple boards**, as tabs along the top: "+" makes a new one (it
  starts with To Do, In Progress and Done columns); double-click a tab to
  rename it, right-click for Rename / Delete. Each board has its own
  columns, cards and labels; the theme, background and swimlanes setting
  apply to all of them. The app reopens on the board you last had open.
- Add / edit / delete cards (title, description, due date, action date, labels, sub-tasks, attachments)
- **Action dates**: the day (or range of days) you plan to work on a card,
  shown on the card as e.g. "Action: Mon 20 Oct – Fri 24 Oct".
- **Blockers & links**: in the card dialog's Links section, mark a card
  as *Blocked by* / *Blocking* another card, or just *Related to* it.
  A card shows "Blocked by: …" in red until its blockers reach a Done
  column. Dragging a blocked card out of the first column asks before
  moving it ("Move it anyway?"), and loops (A blocks B blocks A) are
  refused. In the Gantt view blockers are drawn as arrows, red when the
  blocked card is scheduled to start before its blocker finishes.
- **Gantt view**: "Kanban | Gantt" at the right of the toolbar switches to
  a timeline of every dated card. A card's bar covers its action date(s);
  a card with only a due date runs from the day it was created to its due
  date; a diamond marks the due date when a card has both. Bars use the
  card's label colour (extra labels show as a band along the bottom),
  cards in Done are faded, and the label filter applies here too. Hover
  for details; click a bar to pop the card up (tick sub-tasks, or Edit).
  Click and drag anywhere on the chart to move it around (up/down and
  through time). Drag the grip at either end of a bar to change
  the card's dates (snaps to whole days; Esc cancels mid-drag): on a bar
  that follows the action date, the start/end grips change the action
  start/end; on a due-date-only bar (which runs from the card's creation)
  the end grip moves the due date. Scroll wheel moves through time, Shift + wheel scrolls up and down,
  and Ctrl + wheel zooms in and out around the mouse (from ~9 months
  down to a few days across the screen; rows keep their height). The
  zoom level is remembered.
- **Sub-tasks**: give any card a checklist of sub-tasks. They show on the
  card itself, and ticking one right there strikes it through and saves
  it, with no need to open the card.
- **Links in descriptions**: any web address in a card's description
  (`https://…` or `www.…`) is a clickable link that opens in your browser.
  Teams links (meeting join links, etc.) open in your Teams app instead,
  if you have one — signed in, in its own window (see *Teams links* below).
- **Attachments**: drag files from your file manager onto a card to attach
  them. Each shows as its file-type icon with the name below; click it to
  open the file in its default app. The card links to the original file
  (nothing is copied), so if you move or rename the file the tile greys
  out — hover it to see the old path.
- **Calendar sync**: new events in your Outlook, Google or other calendar
  appear as cards in the "Calendar" column — a different calendar for each
  board if you like (see *Calendar sync* below).
- **Light / dark theme**: "Appearance" in the toolbar → Match system,
  Light or Dark. "Match system" follows your desktop's light/dark setting,
  switching straight away if you change it. Cards stay white in both.
- **Background image**: "Appearance" in the toolbar → "Choose image..."
  puts a picture behind the Kanban board and the Gantt view.
- **Swimlanes**: "Swimlanes" (toolbar, next to Kanban | Gantt) splits the
  board into a band per label, with a line across the board between
  bands; "No Swimlanes" puts it back.
- Add new columns, and **reorder** them by dragging a column's title (its
  cards go with it)
- Drag-and-drop cards between columns, and reorder within a column
- **Due dates**: cards with a due or action date automatically sort to the
  top of their column, earliest first (see *Card order* below). Cards
  without dates sit below, in the order you last placed them. The due date shows on
  the card like the action date, with how far off it is ("Due: Tue 20 Oct
  · 3 days"): green when it's coming up, amber on the day ("· Today"), and
  red "Overdue: … · 2 days ago" after it. Action dates get the same
  countdown ("· Today" while an action range is under way). Cards in a done
  column skip the countdown. The counts update by themselves at midnight.
- **Labels**: create color-coded labels ("Manage Labels" in the toolbar),
  assign any number to a card, and each labeled card shows a colored
  strip + label names. Filter the whole board by label from the "Filter"
  dropdown in the toolbar (shows cards matching *any* selected label;
  "Clear filter" or deselecting everything shows all cards again).
- Everything is saved to a local SQLite database automatically —
  close the app and reopen it, your board is exactly as you left it.
- If you're upgrading from the v1 skeleton, your existing database is
  migrated automatically the first time you run this version — no data
  is lost, it just gains the new `due_date` column and label tables.

## Card order, and dragging

In each column, cards with a date come first, by their earliest date —
the due date or the action date (an action range's first day), whichever
is sooner. On the same day, due dates go above action dates. Cards with
no dates come last, in the order you've dragged them into.

So once a card has a date, its place in the column is set by that date:
dragging it to a different spot in the same column won't "stick", as the
column re-sorts on every refresh. Dragging it to a *different column*
works normally (it takes its date's place there).

## Install

Either way, run these from this folder.

**Option A — install it as a program** (simplest; needs
[pipx](https://pipx.pypa.io), which most distros package as `pipx`):

```bash
pipx install .
./install.sh        # adds "Bonsai" to your application menu, with its icon
```

Run it from the menu, or with `bonsai` in a terminal. To update after
changing the code: `pipx install --force .`

**Option B — run it from this folder** (handy while working on the code):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py   # run it
./install.sh              # optional: add it to the application menu
```

`./install.sh --uninstall` removes the menu entry again (your board is
kept). The menu entry uses the venv in this folder if there is one, else
the `bonsai` command; re-run `./install.sh` if you move the folder or
switch between the two.

## Where your data lives

`~/.local/share/bonsai-kanban/` (or `$XDG_DATA_HOME/bonsai-kanban/` if you've
set that), separately for each user account. `kanban.db` holds **all** your
boards — one plain SQLite file you can back up by copying it (making a
board doesn't create a new file). Deleting a board also compacts the file,
so it never stays bigger than what's in it. Your background image, if you
set one, is kept next to it.

If you used the app before it was called Bonsai, your boards stay where they
were, in `~/.local/share/kanban-app/`, and are found there automatically.

## How to use it

- **Boards**: click a tab to switch; "+" for a new board; double-click a
  tab to rename it; right-click a tab → "Delete board..." (asks first;
  deletes everything on it; the last board can't be deleted)
- **Add a card**: click "+ Add card" at the bottom of any column, or
  right-click anywhere in a column (on a card, empty space or the column's
  title) → "Add card..."
- **Edit a card**: double-click it (change title, description, due date, action date, labels, sub-tasks)
- **Action date**: tick "Action date" and pick a day; tick "to" as well
  and pick an end day for a multi-day task.
- **Sub-tasks**: in the card dialog, "+ Add sub-task" adds a row; Enter in a
  sub-task adds another just below it; ✕ removes one. Tick them off on
  the board.
- **Attach files**: drag them onto a card; or copy them in your file
  manager, select the card and press Ctrl+V; or right-click the card →
  "Attach files..." / "Paste files". In the card dialog, the Attachments
  section lists them (click to open, ✕ to remove — the file itself is
  kept), and you can drop files anywhere on the dialog too.
- **Delete a card**: right-click it → "Delete card"
- **Move a card**: click and drag it to another column (or reorder
  undated cards within a column)
- **Add a column**: "+ Add Column" in the toolbar
- **Reorder columns**: drag a column by its title bar and drop it where you
  want it (a blue line shows where it will land), or right-click the title
  → "Move left" / "Move right". Note the *first* column counts as "not
  started" for blocker warnings, so whichever column you put first takes
  that role.
- **Create/edit/delete labels**: "Manage Labels" in the toolbar
- **Theme**: toolbar "Appearance" → Match system / Light / Dark.
- **Background image**: toolbar "Appearance" → "Choose image..." / "Remove
  background". The image fills the board (cropped to fit, not stretched)
  and stays still while you scroll; columns (and the Gantt chart) get a
  tint over it so they stay readable. Two sliders in the Appearance menu
  set how solid that tint is, separately for the Kanban columns and the
  Gantt chart (0% = clear, 100% = solid); changes show straight away. A
  copy of the image is saved next to the database, so the original can be
  moved or deleted.
- **Filter by label**: "Filter" dropdown in the toolbar, tick one or more
  labels
- **Search**: type in the search box in the middle of the toolbar (or press
  Ctrl+F). Only
  cards containing every word you type stay visible — matched in their
  title, description, sub-tasks, labels or attached files' names, ignoring
  case — with the matches highlighted, and the number of matching cards
  shown beside the box. It searches the board that's open, works with the
  label filter, swimlanes and the Gantt view, and Esc (or the ✕) clears it.
- **Swimlanes**: click "Swimlanes" in the toolbar, next to Kanban | Gantt
  (it then reads "No Swimlanes"; click again to turn them off). The board
  is split into a band per label, in label order, with unlabelled cards
  last, and a thick line runs right across the board between bands. Each
  band is as tall as it needs to be in its fullest column, so the bands
  line up, and the columns scroll together. A card with several labels
  sits in the lane of its first label alphabetically (the one that also
  colours its Gantt bar). Only labels that some card is in get a lane.
  Cards still move between columns as usual; dragging one into a
  different lane doesn't change its labels, so it stays in its own lane.
  The setting is remembered.

## Calendar sync

Works with any calendar that can be published as an `.ics` link:

1. Get the link:
   - **Outlook on the web**: Settings → Calendar → Shared calendars →
     **Publish a calendar**, choose "Can view all details", and copy the
     **ICS** link.
   - **Google Calendar**: Settings → your calendar → Integrate calendar →
     **Secret address in iCal format**.
   - Others (Nextcloud, Fastmail, ...): look for a "subscribe", "share" or
     "iCal/ICS" link.
2. In the app: open the board you want the events on, then toolbar
   **Calendar** → **Set calendar link for this board...** and paste it.

**Each board has its own calendar** (or none) — e.g. a Work board synced
from your Outlook calendar and a Personal board from your Google one. The
Calendar menu is always about the board that's open: its link, "Sync now",
and when it last synced. Every board's calendar is checked in the
background, whichever board you're looking at. Deleting a board deletes its
calendar link too.

The app checks the calendar when it starts and every 5 minutes while it's
open (or **Calendar → Sync now**); the same menu shows when it last
synced. Each one-off event becomes a card in the "Calendar" column (made
for you if it doesn't exist), with the event's day(s) as its action date
and the time, Teams or Google Meet join link (plus Teams' meeting
ID/passcode) and invite text in the description.

- **Changed events** (rescheduled, renamed, new details) update their card.
  Edits you make to a calendar card are kept until the event itself changes.
- **Cancelled events** get a red "Cancelled" label. Calendars (Outlook in
  particular) often remove a cancelled meeting from the published calendar
  rather than marking it, so
  an event that disappears before it starts is treated as cancelled. If it
  comes back, the label is removed.
- **Not imported**: recurring events, events already over when first seen,
  and events whose card you've deleted (they won't come back).
- Calendar services only refresh the published calendar every so often
  (Outlook and Google can take hours), so new invites can take a while to
  show up — that delay is on their side.
- The link is private (anyone with it can see your calendar); it's stored
  in your board's database, not in the code.

## Teams links

Clicking a Teams link on a card (e.g. a meeting's Join link) opens it in
your Teams app, if you have one, so you're already signed in; otherwise it
opens in your web browser. The app looks through your installed
applications for one called Teams that can open links:

- a web app made with Linux Mint's **Web Apps** tool
- Teams **installed as an app from Chrome, Chromium, Edge or Brave** (the
  link opens as an app window in that same browser profile)
- a **GNOME Web** web app, or a **Firefox PWA**
- a native app such as **teams-for-linux**

**Calendar → Teams links...** shows which one it found, and lets you set
your own command instead (with `{url}` where the link goes, e.g.
`brave-browser --app={url}`).

## Project layout

```
main.py          - entry point
pyproject.toml   - package details, for "pipx install ."
install.sh       - adds the app to the desktop's application menu
database.py      - SQLite schema, migrations, all persistence functions
widgets.py       - CardWidget, CardListWidget (drag/drop), ColumnWidget
card_dialog.py   - the add/edit card popup (title, description, dates, labels, sub-tasks, links, attachments)
gantt_view.py    - the Gantt timeline view and the card popup it opens
calendar_sync.py - calendar (.ics link) -> cards in the Calendar column
links.py         - opening clicked links (Teams links in a Teams app)
label_dialog.py  - the "Manage Labels" popup + a shared color-swatch icon helper
theme.py         - light / dark / match-system palettes
main_window.py   - wires everything together, owns the board layout, toolbar, filter
```

## Notes on the drag-and-drop implementation

Qt's built-in drag-and-drop between separate list widgets doesn't play
nicely with custom card widgets, so this implements it manually: a card
press-and-drag starts a `QDrag` carrying the card's id as custom MIME
data, and the column you drop it on reads that back out. After a drop,
we just update SQLite and fully refresh both the source and target
columns from the database — simpler and more robust than trying to keep
the on-screen list widgets perfectly in sync during the drag itself.

## What's next (in order)

1. Archiving (instead of hard delete)
2. A per-card history log
3. Search across all boards at once
