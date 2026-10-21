"""
SQLite persistence layer for the kanban app.

Schema:
    boards(id, name, calendar_url)
    columns(id, board_id, name, position)
    cards(id, column_id, title, description, due_date, action_start, action_end,
          position, created_at, updated_at)
    labels(id, board_id, name, color)
    card_labels(card_id, label_id)
    subtasks(id, card_id, text, done, position)
    card_links(from_card_id, to_card_id, kind)
    attachments(id, card_id, path, position)
    calendar_events(board_id, uid, card_id, fingerprint, start_date, cancelled)
    settings(key, value)

Design note: rather than incrementally shifting positions on every move,
`reorder_column()` takes the full, final ordering of card ids for a column
and rewrites positions 0..n (and column_id) for exactly those cards. This
is simple to reason about and hard to get subtly wrong, at the cost of a
few more UPDATE statements per drag-and-drop -- a non-issue at this scale.

Ordering note: cards with a date (due or action) sort to the top, by their
earliest date, with due dates first on the same day; `position` only
decides the order of undated cards, and of ties -- see _card_order().

Action dates: `action_start`/`action_end` are the day(s) the work is planned
for. A single-day action has action_end == action_start; no action date
means both are NULL.

Links: a card_links row with kind 'blocks' means from_card must be done
before to_card can start. kind 'relates' is a plain two-way "these go
together" link, stored once in either direction.

Attachments: links to local files (or folders) by absolute path. The file
itself isn't copied, so moving or renaming it breaks the attachment.

Calendar: each board can sync its own calendar (boards.calendar_url).
calendar_events has one row per event turned into a card, per board (see
calendar_sync.py). card_id becomes NULL if the card is deleted, which stops
the event from being imported again.
"""

import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DATA_FOLDER = "bonsai-kanban"
OLD_DATA_FOLDERS = ["kanban-app"]  # what it was called before the app was named Bonsai


def _data_dir() -> Path:
    """Where the boards live: bonsai-kanban/ in the user's data folder
    ($XDG_DATA_HOME, normally ~/.local/share).

    An existing board is never left behind: if there's none there yet but
    there is one in the old kanban-app/ folder, or in ~/.local/share when
    XDG_DATA_HOME points somewhere else (older versions always used it),
    that one is used instead.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "")
    bases = [Path(xdg)] if xdg and Path(xdg).is_absolute() else []
    bases.append(Path.home() / ".local" / "share")
    for base in bases:
        for name in [DATA_FOLDER, *OLD_DATA_FOLDERS]:
            if (base / name / "kanban.db").exists():
                return base / name
    return bases[0] / DATA_FOLDER


DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "kanban.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS boards (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS columns (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
            name     TEXT NOT NULL,
            position INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cards (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id   INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            description TEXT DEFAULT '',
            due_date    TEXT,
            action_start TEXT,
            action_end  TEXT,
            position    INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS labels (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
            name     TEXT NOT NULL,
            color    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_labels (
            card_id  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
            PRIMARY KEY (card_id, label_id)
        );

        CREATE TABLE IF NOT EXISTS subtasks (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            text     TEXT NOT NULL,
            done     INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_links (
            from_card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            to_card_id   INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            kind         TEXT NOT NULL CHECK (kind IN ('blocks', 'relates')),
            PRIMARY KEY (from_card_id, to_card_id, kind)
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            path     TEXT NOT NULL,
            position INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS calendar_events (
            board_id    INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
            uid         TEXT NOT NULL,
            card_id     INTEGER REFERENCES cards(id) ON DELETE SET NULL,
            fingerprint TEXT NOT NULL,
            start_date  TEXT NOT NULL,
            cancelled   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (board_id, uid)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()

    # Migration: databases created before these columns existed won't have
    # them yet, since CREATE TABLE IF NOT EXISTS above is a no-op for a
    # table that already exists. Add any that are missing so existing
    # boards keep working without losing data.
    existing_columns = {row["name"] for row in cur.execute("PRAGMA table_info(cards)")}
    for column in ("due_date", "action_start", "action_end"):
        if column not in existing_columns:
            cur.execute(f"ALTER TABLE cards ADD COLUMN {column} TEXT")
    if "calendar_url" not in {row["name"] for row in cur.execute("PRAGMA table_info(boards)")}:
        cur.execute("ALTER TABLE boards ADD COLUMN calendar_url TEXT")
    conn.commit()
    _migrate_calendar_to_boards(conn)

    # Seed a default board + columns on first run only.
    has_board = cur.execute("SELECT COUNT(*) FROM boards").fetchone()[0] > 0
    conn.close()
    if not has_board:
        add_board("My Board")


DEFAULT_COLUMNS = ["To Do", "In Progress", "Done"]


def get_boards():
    """Every board, oldest first (the order of the tabs)."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM boards ORDER BY id").fetchall()
    conn.close()
    return rows


def add_board(name: str) -> int:
    """A new board with the usual To Do / In Progress / Done columns."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO boards (name) VALUES (?)", (name,))
    board_id = cur.lastrowid
    for i, column_name in enumerate(DEFAULT_COLUMNS):
        cur.execute(
            "INSERT INTO columns (board_id, name, position) VALUES (?, ?, ?)", (board_id, column_name, i)
        )
    conn.commit()
    conn.close()
    return board_id


def rename_board(board_id: int, name: str) -> None:
    conn = get_connection()
    conn.execute("UPDATE boards SET name = ? WHERE id = ?", (name, board_id))
    conn.commit()
    conn.close()


def delete_board(board_id: int) -> None:
    """Delete a board with everything on it: its columns, cards (and their
    sub-tasks, links and attachments), labels, and calendar link and events.

    Then compacts the database file. SQLite keeps the space deleted rows
    took up for reuse rather than shrinking the file; VACUUM gives it back,
    so deleting boards never leaves the file bigger than its contents need.
    """
    conn = get_connection()
    conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))  # the rest goes with it (ON DELETE CASCADE)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()


def _migrate_calendar_to_boards(conn: sqlite3.Connection) -> None:
    """Older versions had one calendar for the whole app: its link in the
    settings table, and calendar_events with no board. Now each board has
    its own. Move both onto the board that calendar was syncing into (the
    "calendar_board" setting, else the first board). Does nothing once done.
    """
    event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(calendar_events)")}
    old_url = conn.execute("SELECT value FROM settings WHERE key = 'calendar_ics_url'").fetchone()
    if "board_id" in event_columns and old_url is None:
        return
    boards = [row["id"] for row in conn.execute("SELECT id FROM boards ORDER BY id")]
    if not boards:
        return
    chosen = conn.execute("SELECT value FROM settings WHERE key = 'calendar_board'").fetchone()
    board_id = int(chosen["value"]) if chosen and str(chosen["value"]).isdigit() and int(chosen["value"]) in boards else boards[0]

    if "board_id" not in event_columns:
        # SQLite can't change a primary key in place: build the new table,
        # copy the rows across (each onto its card's board, or the calendar's
        # board for events whose card was deleted), then swap it in.
        conn.executescript(
            f"""
            CREATE TABLE calendar_events_new (
                board_id    INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                uid         TEXT NOT NULL,
                card_id     INTEGER REFERENCES cards(id) ON DELETE SET NULL,
                fingerprint TEXT NOT NULL,
                start_date  TEXT NOT NULL,
                cancelled   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (board_id, uid)
            );
            INSERT INTO calendar_events_new (board_id, uid, card_id, fingerprint, start_date, cancelled)
                SELECT COALESCE(columns.board_id, {board_id}), ce.uid, ce.card_id, ce.fingerprint,
                       ce.start_date, ce.cancelled
                FROM calendar_events ce
                LEFT JOIN cards ON cards.id = ce.card_id
                LEFT JOIN columns ON columns.id = cards.column_id;
            DROP TABLE calendar_events;
            ALTER TABLE calendar_events_new RENAME TO calendar_events;
            """
        )
    if old_url is not None:
        if old_url["value"]:
            conn.execute("UPDATE boards SET calendar_url = ? WHERE id = ?", (old_url["value"], board_id))
        conn.execute("DELETE FROM settings WHERE key IN ('calendar_ics_url', 'calendar_board')")
    conn.commit()


def get_default_board_id() -> int:
    conn = get_connection()
    row = conn.execute("SELECT id FROM boards ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return row["id"]


def get_columns(board_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM columns WHERE board_id = ? ORDER BY position", (board_id,)
    ).fetchall()
    conn.close()
    return rows


def reorder_columns(ordered_column_ids: list[int]) -> None:
    """Rewrite column positions 0..n in the given order. Cards belong to
    their column, so they move along with it.
    """
    conn = get_connection()
    for pos, column_id in enumerate(ordered_column_ids):
        conn.execute("UPDATE columns SET position = ? WHERE id = ?", (pos, column_id))
    conn.commit()
    conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str | None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_column(column_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM columns WHERE id = ?", (column_id,)).fetchone()
    conn.close()
    return row


def add_column(board_id: int, name: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    next_pos = cur.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM columns WHERE board_id = ?",
        (board_id,),
    ).fetchone()[0]
    cur.execute(
        "INSERT INTO columns (board_id, name, position) VALUES (?, ?, ?)",
        (board_id, name, next_pos),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def delete_column(column_id: int) -> None:
    """Delete a column and its cards (and their sub-tasks, links and
    attachments), then close the gap in the remaining columns' positions.
    """
    conn = get_connection()
    row = conn.execute("SELECT board_id FROM columns WHERE id = ?", (column_id,)).fetchone()
    if row is None:
        conn.close()
        return
    conn.execute("DELETE FROM columns WHERE id = ?", (column_id,))  # its cards go with it (ON DELETE CASCADE)
    remaining = conn.execute(
        "SELECT id FROM columns WHERE board_id = ? ORDER BY position", (row["board_id"],)
    ).fetchall()
    for pos, column in enumerate(remaining):
        conn.execute("UPDATE columns SET position = ? WHERE id = ?", (pos, column["id"]))
    conn.commit()
    conn.close()


def _card_order(table: str = "") -> str:
    """The ORDER BY for cards in a column (table: e.g. "cards." when joined):

    1. Cards with a date first, by their earliest one -- the due date or the
       action date (an action range's first day), whichever comes sooner.
    2. On the same day, due dates before action dates.
    3. Undated cards last.
    4. Otherwise (undated cards, and ties) the order they were dragged into.

    Dates are "yyyy-mm-dd" text, which sorts in date order; "9999" stands in
    for a missing date so it sorts after every real one.
    """
    due = f"COALESCE(NULLIF({table}due_date, ''), '9999')"
    action = f"COALESCE(NULLIF({table}action_start, ''), '9999')"
    return f"ORDER BY MIN({due}, {action}), ({due} > {action}), {table}position"


def get_cards(column_id: int, label_ids: list[int] | None = None):
    """Cards in a column, in board order (see _card_order). If label_ids is
    given, only cards with at least one of those labels are returned (still
    in the same order).
    """
    conn = get_connection()
    if label_ids:
        placeholders = ",".join("?" for _ in label_ids)
        rows = conn.execute(
            f"""
            SELECT DISTINCT cards.* FROM cards
            JOIN card_labels ON card_labels.card_id = cards.id
            WHERE cards.column_id = ? AND card_labels.label_id IN ({placeholders})
            {_card_order("cards.")}
            """,
            (column_id, *label_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM cards WHERE column_id = ? {_card_order()}",
            (column_id,),
        ).fetchall()
    conn.close()
    return rows


def search_words(query: str) -> list[str]:
    """A search box's text as lower-case words ("Budget  client" -> both)."""
    return query.lower().split()


def filter_cards_by_search(cards, words: list[str]) -> list:
    """The cards (in the same order) that contain every one of the words,
    anywhere in their title, description, sub-tasks, label names or attached
    files' names. Case doesn't matter; no words means every card.
    """
    if not words:
        return list(cards)
    card_ids = [card["id"] for card in cards]
    labels = get_labels_for_cards(card_ids)
    subtasks = get_subtasks_for_cards(card_ids)
    attachments = get_attachments_for_cards(card_ids)

    def searchable(card) -> str:
        parts = [card["title"], card["description"] or ""]
        parts += [label["name"] for label in labels.get(card["id"], [])]
        parts += [subtask["text"] for subtask in subtasks.get(card["id"], [])]
        parts += [os.path.basename(a["path"].rstrip("/")) for a in attachments.get(card["id"], [])]
        return "\n".join(parts).lower()

    return [card for card in cards if all(word in searchable(card) for word in words)]


def get_board_cards(board_id: int, label_ids: list[int] | None = None):
    """Every card on a board, each with its column's name as `column_name`.
    If label_ids is given, only cards with at least one of those labels.
    """
    conn = get_connection()
    sql = """
        SELECT cards.*, columns.name AS column_name FROM cards
        JOIN columns ON columns.id = cards.column_id
        WHERE columns.board_id = ?
    """
    params: list = [board_id]
    if label_ids:
        placeholders = ",".join("?" for _ in label_ids)
        sql += f""" AND EXISTS (
            SELECT 1 FROM card_labels
            WHERE card_labels.card_id = cards.id AND card_labels.label_id IN ({placeholders})
        )"""
        params += label_ids
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def get_card(card_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    return row


def add_card(
    column_id: int,
    title: str,
    description: str = "",
    due_date: str | None = None,
    action_start: str | None = None,
    action_end: str | None = None,
) -> int:
    conn = get_connection()
    cur = conn.cursor()
    next_pos = cur.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM cards WHERE column_id = ?",
        (column_id,),
    ).fetchone()[0]
    now = _now()
    cur.execute(
        """INSERT INTO cards (column_id, title, description, due_date, action_start, action_end,
                              position, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (column_id, title, description, due_date, action_start, action_end, next_pos, now, now),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_card(
    card_id: int,
    title: str,
    description: str,
    due_date: str | None = None,
    action_start: str | None = None,
    action_end: str | None = None,
) -> None:
    conn = get_connection()
    conn.execute(
        """UPDATE cards SET title = ?, description = ?, due_date = ?,
                            action_start = ?, action_end = ?, updated_at = ?
           WHERE id = ?""",
        (title, description, due_date, action_start, action_end, _now(), card_id),
    )
    conn.commit()
    conn.close()


def set_action_dates(card_id: int, action_start: str, action_end: str) -> None:
    """Change just a card's action date(s), e.g. from dragging its Gantt bar."""
    conn = get_connection()
    conn.execute(
        "UPDATE cards SET action_start = ?, action_end = ?, updated_at = ? WHERE id = ?",
        (action_start, action_end, _now(), card_id),
    )
    conn.commit()
    conn.close()


def set_due_date(card_id: int, due_date: str) -> None:
    """Change just a card's due date, e.g. from dragging its Gantt bar."""
    conn = get_connection()
    conn.execute("UPDATE cards SET due_date = ?, updated_at = ? WHERE id = ?", (due_date, _now(), card_id))
    conn.commit()
    conn.close()


def delete_card(card_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    conn.close()


def get_labels(board_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM labels WHERE board_id = ? ORDER BY name COLLATE NOCASE", (board_id,)
    ).fetchall()
    conn.close()
    return rows


def add_label(board_id: int, name: str, color: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO labels (board_id, name, color) VALUES (?, ?, ?)",
        (board_id, name, color),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_label(label_id: int, name: str, color: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE labels SET name = ?, color = ? WHERE id = ?", (name, color, label_id)
    )
    conn.commit()
    conn.close()


def delete_label(label_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
    conn.commit()
    conn.close()


def find_or_add_label(board_id: int, name: str, color: str) -> int:
    """The id of the board's label with this name (any case), creating it
    with the given color if there isn't one.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM labels WHERE board_id = ? AND name = ? COLLATE NOCASE", (board_id, name)
    ).fetchone()
    conn.close()
    return row["id"] if row else add_label(board_id, name, color)


def set_card_label(card_id: int, label_id: int, on: bool) -> None:
    """Add or remove one label on a card, leaving its other labels alone."""
    conn = get_connection()
    if on:
        conn.execute(
            "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)", (card_id, label_id)
        )
    else:
        conn.execute("DELETE FROM card_labels WHERE card_id = ? AND label_id = ?", (card_id, label_id))
    conn.commit()
    conn.close()


def get_card_labels(card_id: int):
    conn = get_connection()
    rows = conn.execute(
        """SELECT labels.* FROM labels
           JOIN card_labels ON card_labels.label_id = labels.id
           WHERE card_labels.card_id = ?
           ORDER BY labels.name COLLATE NOCASE""",
        (card_id,),
    ).fetchall()
    conn.close()
    return rows


def get_labels_for_cards(card_ids: list[int]) -> dict[int, list]:
    """Labels for many cards in one query, as {card_id: [label rows]}.
    Cards with no labels are simply absent from the dict.
    """
    if not card_ids:
        return {}
    placeholders = ",".join("?" for _ in card_ids)
    conn = get_connection()
    rows = conn.execute(
        f"""SELECT card_labels.card_id, labels.* FROM labels
            JOIN card_labels ON card_labels.label_id = labels.id
            WHERE card_labels.card_id IN ({placeholders})
            ORDER BY labels.name COLLATE NOCASE""",
        card_ids,
    ).fetchall()
    conn.close()
    result: dict[int, list] = {}
    for row in rows:
        result.setdefault(row["card_id"], []).append(row)
    return result


def set_card_labels(card_id: int, label_ids: list[int]) -> None:
    """Replace a card's label assignments wholesale with the given set."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM card_labels WHERE card_id = ?", (card_id,))
    for label_id in label_ids:
        cur.execute(
            "INSERT INTO card_labels (card_id, label_id) VALUES (?, ?)",
            (card_id, label_id),
        )
    conn.commit()
    conn.close()


def get_subtasks(card_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM subtasks WHERE card_id = ? ORDER BY position", (card_id,)
    ).fetchall()
    conn.close()
    return rows


def get_subtasks_for_cards(card_ids: list[int]) -> dict[int, list]:
    """Sub-tasks for many cards in one query, as {card_id: [subtask rows]}.
    Cards with no sub-tasks are simply absent from the dict.
    """
    if not card_ids:
        return {}
    placeholders = ",".join("?" for _ in card_ids)
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM subtasks WHERE card_id IN ({placeholders}) ORDER BY position",
        card_ids,
    ).fetchall()
    conn.close()
    result: dict[int, list] = {}
    for row in rows:
        result.setdefault(row["card_id"], []).append(row)
    return result


def set_card_subtasks(card_id: int, subtasks: list[tuple[str, bool]]) -> None:
    """Replace a card's sub-tasks wholesale with the given (text, done) list,
    in order -- same approach as set_card_labels().
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM subtasks WHERE card_id = ?", (card_id,))
    for pos, (text, done) in enumerate(subtasks):
        cur.execute(
            "INSERT INTO subtasks (card_id, text, done, position) VALUES (?, ?, ?, ?)",
            (card_id, text, int(done), pos),
        )
    conn.commit()
    conn.close()


def set_subtask_done(subtask_id: int, done: bool) -> None:
    """Tick or untick one sub-task (used by the checkbox on the card itself)."""
    conn = get_connection()
    conn.execute("UPDATE subtasks SET done = ? WHERE id = ?", (int(done), subtask_id))
    conn.execute(
        "UPDATE cards SET updated_at = ? WHERE id = (SELECT card_id FROM subtasks WHERE id = ?)",
        (_now(), subtask_id),
    )
    conn.commit()
    conn.close()


def get_links_for_cards(card_ids: list[int]) -> dict[int, list[dict]]:
    """Every link touching the given cards, seen from each card's side, as
    {card_id: [{"kind", "other_id", "other_title", "other_column"}]} where
    kind is "blocked_by", "blocks" or "relates". Cards with no links are
    simply absent from the dict.
    """
    if not card_ids:
        return {}
    placeholders = ",".join("?" for _ in card_ids)
    conn = get_connection()
    rows = conn.execute(
        f"""SELECT l.from_card_id, l.to_card_id, l.kind,
                   f.title AS from_title, fc.name AS from_column,
                   t.title AS to_title, tc.name AS to_column
            FROM card_links l
            JOIN cards f ON f.id = l.from_card_id
            JOIN columns fc ON fc.id = f.column_id
            JOIN cards t ON t.id = l.to_card_id
            JOIN columns tc ON tc.id = t.column_id
            WHERE l.from_card_id IN ({placeholders}) OR l.to_card_id IN ({placeholders})
            ORDER BY l.kind, f.title COLLATE NOCASE, t.title COLLATE NOCASE""",
        [*card_ids, *card_ids],
    ).fetchall()
    conn.close()
    wanted = set(card_ids)
    result: dict[int, list[dict]] = {}
    for row in rows:
        if row["from_card_id"] in wanted:
            result.setdefault(row["from_card_id"], []).append({
                "kind": row["kind"],  # "blocks" or "relates"
                "other_id": row["to_card_id"],
                "other_title": row["to_title"],
                "other_column": row["to_column"],
            })
        if row["to_card_id"] in wanted:
            result.setdefault(row["to_card_id"], []).append({
                "kind": "blocked_by" if row["kind"] == "blocks" else "relates",
                "other_id": row["from_card_id"],
                "other_title": row["from_title"],
                "other_column": row["from_column"],
            })
    return result


def get_block_edges(board_id: int) -> list[tuple[int, int]]:
    """All (blocker_id, blocked_id) pairs on a board."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT l.from_card_id, l.to_card_id FROM card_links l
           JOIN cards c ON c.id = l.from_card_id
           JOIN columns col ON col.id = c.column_id
           WHERE l.kind = 'blocks' AND col.board_id = ?""",
        (board_id,),
    ).fetchall()
    conn.close()
    return [(row[0], row[1]) for row in rows]


def set_card_links(card_id: int, links: list[tuple[str, int]]) -> None:
    """Replace every link touching a card with the given (kind, other_card_id)
    list, kind being "blocked_by", "blocks" or "relates" from this card's side.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM card_links WHERE from_card_id = ? OR to_card_id = ?", (card_id, card_id))
    for kind, other_id in links:
        if kind == "blocked_by":
            row = (other_id, card_id, "blocks")
        else:
            row = (card_id, other_id, kind)
        cur.execute(
            "INSERT OR IGNORE INTO card_links (from_card_id, to_card_id, kind) VALUES (?, ?, ?)", row
        )
    conn.commit()
    conn.close()


def get_attachments(card_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM attachments WHERE card_id = ? ORDER BY position", (card_id,)
    ).fetchall()
    conn.close()
    return rows


def get_attachments_for_cards(card_ids: list[int]) -> dict[int, list]:
    """Attachments for many cards in one query, as {card_id: [attachment rows]}.
    Cards with no attachments are simply absent from the dict.
    """
    if not card_ids:
        return {}
    placeholders = ",".join("?" for _ in card_ids)
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM attachments WHERE card_id IN ({placeholders}) ORDER BY position",
        card_ids,
    ).fetchall()
    conn.close()
    result: dict[int, list] = {}
    for row in rows:
        result.setdefault(row["card_id"], []).append(row)
    return result


def add_attachments(card_id: int, paths: list[str]) -> None:
    """Attach files to the end of a card's list, skipping any already attached
    (used when files are dropped or pasted straight onto a card).
    """
    conn = get_connection()
    cur = conn.cursor()
    existing = {row["path"] for row in cur.execute("SELECT path FROM attachments WHERE card_id = ?", (card_id,))}
    next_pos = cur.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM attachments WHERE card_id = ?", (card_id,)
    ).fetchone()[0]
    for path in paths:
        if path in existing:
            continue
        existing.add(path)
        cur.execute(
            "INSERT INTO attachments (card_id, path, position) VALUES (?, ?, ?)",
            (card_id, path, next_pos),
        )
        next_pos += 1
    cur.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (_now(), card_id))
    conn.commit()
    conn.close()


def set_card_attachments(card_id: int, paths: list[str]) -> None:
    """Replace a card's attachments wholesale with the given paths, in order
    -- same approach as set_card_subtasks().
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM attachments WHERE card_id = ?", (card_id,))
    for pos, path in enumerate(paths):
        cur.execute(
            "INSERT INTO attachments (card_id, path, position) VALUES (?, ?, ?)",
            (card_id, path, pos),
        )
    conn.commit()
    conn.close()


def get_calendar_url(board_id: int) -> str | None:
    """The .ics link this board's calendar syncs from, or None."""
    conn = get_connection()
    row = conn.execute("SELECT calendar_url FROM boards WHERE id = ?", (board_id,)).fetchone()
    conn.close()
    return row["calendar_url"] if row else None


def set_calendar_url(board_id: int, url: str | None) -> None:
    conn = get_connection()
    conn.execute("UPDATE boards SET calendar_url = ? WHERE id = ?", (url or None, board_id))
    conn.commit()
    conn.close()


def get_all_calendar_urls() -> list[str]:
    """Every board's calendar link (for the meeting alerter, which runs
    without the app). Empty if there's no database yet; doesn't make one.
    """
    if not DB_PATH.exists():
        return []
    conn = get_connection()
    rows = conn.execute(
        "SELECT calendar_url FROM boards WHERE calendar_url IS NOT NULL AND calendar_url != '' ORDER BY id"
    ).fetchall()
    conn.close()
    return [row["calendar_url"] for row in rows]


def get_calendar_events(board_id: int) -> dict:
    """Every calendar event imported into a board, as {uid: row}."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM calendar_events WHERE board_id = ?", (board_id,)).fetchall()
    conn.close()
    return {row["uid"]: row for row in rows}


def save_calendar_event(
    board_id: int, uid: str, card_id: int | None, fingerprint: str, start_date: str, cancelled: bool
) -> None:
    conn = get_connection()
    conn.execute(
        """INSERT INTO calendar_events (board_id, uid, card_id, fingerprint, start_date, cancelled)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(board_id, uid) DO UPDATE SET card_id = excluded.card_id,
               fingerprint = excluded.fingerprint, start_date = excluded.start_date,
               cancelled = excluded.cancelled""",
        (board_id, uid, card_id, fingerprint, start_date, int(cancelled)),
    )
    conn.commit()
    conn.close()


def reorder_column(column_id: int, ordered_card_ids: list[int]) -> None:
    """Rewrite position (and column_id) for exactly the given cards, in order.

    Used both for a plain in-column reorder and for a cross-column move:
    in the latter case the moved card's id is included in the target
    column's list, which also updates its column_id in one pass.
    """
    conn = get_connection()
    cur = conn.cursor()
    now = _now()
    for pos, card_id in enumerate(ordered_card_ids):
        cur.execute(
            "UPDATE cards SET column_id = ?, position = ?, updated_at = ? WHERE id = ?",
            (column_id, pos, now, card_id),
        )
    conn.commit()
    conn.close()
