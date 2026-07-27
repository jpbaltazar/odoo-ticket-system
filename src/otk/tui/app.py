"""Master/detail ticket inbox for the operator.

The TUI is a pure view over `service.Store`: every read and every mutation goes
through it, so the inbox and the HTTP API can never disagree about a ticket.
Nothing here writes SQL or touches the blob store directly.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from rich.table import Table as RichTable
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.data_table import ColumnKey
from textual.widgets.option_list import Option

from ..schemas import Priority, TicketStatus
from ..service import (
    AttachmentRecord,
    CommentRecord,
    ServiceError,
    Store,
    TicketFilters,
    TicketRecord,
    now,
)

# The upper half-block: one character cell carries two vertical pixels, the top
# one as the foreground colour and the bottom one as the background. This is the
# widest-supported way to draw an image in a terminal — it needs truecolor and
# nothing else, so no kitty/sixel negotiation and no fallback matrix.
HALF_BLOCK = "▀"

PREVIEW_MAX_CELLS_HIGH = 40

STATUS_ORDER: tuple[str, ...] = tuple(status.value for status in TicketStatus)
PRIORITY_ORDER: tuple[str, ...] = tuple(priority.value for priority in Priority)

PRIORITY_STYLES: dict[str, str] = {
    Priority.URGENT: "bold red",
    Priority.HIGH: "dark_orange",
    Priority.NORMAL: "",
    Priority.LOW: "dim",
}

MUTED_STATUSES = {TicketStatus.RESOLVED, TicketStatus.CLOSED}

# (column key, header, width) — the order also drives `_row_cells`. The title
# column is resized to fill whatever the others leave, see `fit_title_column`.
TABLE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("unread", "", 1),
    ("ref", "ref", 11),
    ("priority", "pri", 6),
    ("client", "client", 10),
    ("title", "title", 30),
    ("reporter", "reporter", 12),
    ("age", "age", 3),
)
# Below this the table scrolls sideways rather than shrinking titles to nothing.
MIN_TITLE_WIDTH = 14

FILTER_MODES: tuple[str, ...] = ("all", "open", "unread")
FILTER_LABELS = {"all": "all tickets", "open": "open only", "unread": "unread only"}

# Overridable so the app can be driven on a machine without a desktop session.
IMAGE_OPENER = os.environ.get("OTK_OPENER", "xdg-open")


def operator_name() -> str:
    return os.environ.get("OTK_OPERATOR", "").strip() or "operator"


def format_age(value: datetime | None) -> str:
    if value is None:
        return "-"
    seconds = int((now() - value).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}d"
    return f"{seconds // (86400 * 30)}mo"


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def format_stamp(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "-"


def screenshot_of(ticket: TicketRecord) -> AttachmentRecord | None:
    """Prefer the declared screenshot, else any image hanging off the ticket."""
    for attachment in ticket.attachments:
        if attachment.role == "screenshot":
            return attachment
    for attachment in ticket.attachments:
        if attachment.content_type.startswith("image/"):
            return attachment
    return None


def key_value_grid(pairs: Iterable[tuple[str, Any]]) -> RichTable:
    grid = RichTable.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    empty = True
    for label, value in pairs:
        if value is None or value == "":
            continue
        empty = False
        grid.add_row(Text(label, style="dim"), Text(str(value)))
    if empty:
        grid.add_row(Text("-", style="dim"), Text(""))
    return grid


def render_image_preview(path: Path, cells_wide: int, cells_high: int) -> Text:
    """Downscale an image and draw it as half-block cells.

    Raises whatever Pillow raises; the caller decides how to degrade.
    """
    from PIL import Image

    with Image.open(path) as handle:
        source = handle.convert("RGB")
        width = max(1, min(cells_wide, source.width))
        scaled_height = max(2, round(source.height * (width / source.width) * 0.5) * 2)
        # Two pixel rows per cell, and an even count so the last row has a pair.
        height = min(scaled_height, cells_high * 2)
        height -= height % 2
        image = source.resize((width, max(2, height)))

    pixels = image.load()
    text = Text(no_wrap=True)
    for y in range(0, image.height, 2):
        for x in range(image.width):
            top = pixels[x, y]
            bottom = pixels[x, y + 1]
            text.append(
                HALF_BLOCK,
                f"rgb({top[0]},{top[1]},{top[2]}) on rgb({bottom[0]},{bottom[1]},{bottom[2]})",
            )
        text.append("\n")
    return text


# --------------------------------------------------------------------- modals


class ChoiceScreen(ModalScreen[str | None]):
    """Pick one value from a short list — status, priority, client."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(
        self,
        title: str,
        choices: Sequence[tuple[str, str]],
        current: str | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._choices = choices
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static(self._title, classes="modal-title")
            yield OptionList(
                *(Option(label, id=value) for value, label in self._choices),
                id="choices",
            )

    def on_mount(self) -> None:
        options = self.query_one("#choices", OptionList)
        for index, (value, _) in enumerate(self._choices):
            if value == self._current:
                options.highlighted = index
                break
        options.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PromptScreen(ModalScreen[str | None]):
    """A single-line prompt, used for search and assignment."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, title: str, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-prompt"):
            yield Static(self._title, classes="modal-title")
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ComposeScreen(ModalScreen[str | None]):
    """Multi-line composer for a public reply or an internal note."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "post", "Post", priority=True),
    ]

    def __init__(self, title: str, hint: str) -> None:
        super().__init__()
        self._title = title
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-compose"):
            yield Static(self._title, classes="modal-title")
            yield Static(self._hint, classes="modal-hint")
            yield TextArea(id="body")
            with Horizontal(classes="modal-buttons"):
                yield Button("Post", variant="primary", id="post")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#body", TextArea).focus()

    @on(Button.Pressed, "#post")
    def _post_pressed(self) -> None:
        self.action_post()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_post(self) -> None:
        body = self.query_one("#body", TextArea).text.strip()
        self.dismiss(body or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------- panes


class TicketDetail(VerticalScroll):
    """Everything known about one ticket, rebuilt on each selection."""

    can_focus = True

    async def show(self, ticket: TicketRecord | None, preview: Text | str | None) -> None:
        await self.remove_children()
        if ticket is None:
            await self.mount(Static("No ticket selected.", classes="placeholder"))
            return
        await self.mount_all(list(self._blocks(ticket, preview)))
        self.scroll_home(animate=False)

    def _blocks(self, ticket: TicketRecord, preview: Text | str | None) -> Iterable[Static]:
        yield Static(Text(ticket.title, style="bold"), classes="detail-title")
        yield Static(self._headline(ticket), classes="detail-headline")
        yield Static(
            key_value_grid(
                [
                    ("client", f"{ticket.client_name} ({ticket.client_slug})"),
                    ("reporter", ticket.reporter.get("name")),
                    ("email", ticket.reporter.get("email")),
                    ("login", ticket.reporter.get("login")),
                    ("odoo uid", ticket.reporter.get("odoo_uid")),
                    ("assignee", ticket.assignee),
                    ("tags", ", ".join(ticket.tags)),
                    ("source", ticket.source),
                    ("created", format_stamp(ticket.created_at)),
                    ("updated", format_stamp(ticket.updated_at)),
                ]
            ),
            classes="detail-meta",
        )

        yield Static("Description", classes="section")
        yield Static(Text(ticket.description or "(no description)"), classes="body")

        yield from self._context_blocks(ticket.context or {})
        yield from self._attachment_blocks(ticket, preview)
        yield from self._comment_blocks(ticket.comments)

    def _headline(self, ticket: TicketRecord) -> Text:
        text = Text()
        text.append(ticket.ref, style="bold")
        text.append("  ")
        text.append(ticket.status, style="reverse")
        text.append("  ")
        text.append(ticket.priority, style=PRIORITY_STYLES.get(ticket.priority, ""))
        text.append("  ")
        text.append(ticket.category, style="dim")
        return text

    def _context_blocks(self, context: dict[str, Any]) -> Iterable[Static]:
        yield Static("Odoo context", classes="section")
        browser = context.get("browser") or {}
        yield Static(
            key_value_grid(
                [
                    ("url", context.get("url")),
                    ("page", context.get("page_title")),
                    ("database", context.get("database")),
                    ("odoo version", context.get("odoo_version")),
                    ("model", context.get("model")),
                    ("res id", context.get("res_id")),
                    ("view type", context.get("view_type")),
                    ("company", context.get("company_name") or context.get("company_id")),
                    ("lang / tz", " / ".join(
                        part for part in (context.get("user_lang"), context.get("user_tz")) if part
                    )),
                    ("action", context.get("action_xml_id") or context.get("action_id")),
                    ("browser", browser.get("user_agent")),
                ]
            ),
            classes="detail-meta",
        )

        error = context.get("error") or {}
        if not error:
            return
        yield Static("Error", classes="section section-error")
        yield Static(
            key_value_grid(
                [
                    ("name", error.get("name")),
                    ("message", error.get("message")),
                    ("route", error.get("rpc_route")),
                    ("model", error.get("rpc_model")),
                    ("method", error.get("rpc_method")),
                    ("http status", error.get("http_status")),
                ]
            ),
            classes="detail-error",
        )
        for label, key in (("Traceback", "traceback"), ("Server traceback", "server_traceback")):
            trace = error.get(key)
            if trace:
                yield Collapsible(
                    Static(Text(trace), classes="traceback"),
                    title=label,
                    collapsed=True,
                )

    def _attachment_blocks(
        self, ticket: TicketRecord, preview: Text | str | None
    ) -> Iterable[Static]:
        yield Static("Attachments", classes="section")
        if not ticket.attachments:
            yield Static(Text("none", style="dim"), classes="body")
        else:
            listing = Text()
            for attachment in ticket.attachments:
                listing.append(f"{attachment.role:<11}", style="dim")
                listing.append(attachment.filename)
                detail = f"  {format_size(attachment.size_bytes)}"
                if attachment.width and attachment.height:
                    detail += f"  {attachment.width}x{attachment.height}"
                listing.append(detail, style="dim")
                listing.append("\n")
            yield Static(listing, classes="body")
        if preview is not None:
            yield Static(preview, classes="preview")

    def _comment_blocks(self, comments: Sequence[CommentRecord]) -> Iterable[Static]:
        yield Static("Thread", classes="section")
        if not comments:
            yield Static(Text("no comments yet", style="dim"), classes="body")
            return
        for comment in comments:
            internal = comment.visibility == "internal"
            header = Text()
            header.append(comment.author_name, style="bold")
            header.append(f"  ({comment.author_type})", style="dim")
            header.append(f"  {format_stamp(comment.created_at)}", style="dim")
            if internal:
                header.append("  internal note", style="bold yellow")
            body = Text()
            body.append_text(header)
            body.append("\n")
            body.append(comment.body)
            yield Static(body, classes=f"comment {'internal' if internal else 'public'}")


# ----------------------------------------------------------------------- app


class TicketApp(App[None]):
    """The inbox itself."""

    CSS_PATH = "app.tcss"
    TITLE = "otk inbox"

    POLL_SECONDS = 10.0

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("g", "goto_top", "Top", show=False),
        Binding("G", "goto_bottom", "Bottom", show=False),
        Binding("enter", "focus_detail", "Detail", show=False),
        Binding("o", "open_screenshot", "Open"),
        Binding("v", "toggle_preview", "Preview"),
        Binding("s", "choose_status", "Status"),
        Binding("p", "choose_priority", "Priority"),
        Binding("r", "reply", "Reply"),
        Binding("n", "note", "Note"),
        Binding("a", "assign", "Assign"),
        Binding("slash", "search", "Search"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("c", "choose_client", "Client"),
        Binding("u", "mark_unread", "Unread"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, store: Store | None = None) -> None:
        super().__init__()
        self.store = store or Store()
        self.operator = operator_name()
        self.tickets: list[TicketRecord] = []
        self.filter_mode = "all"
        self.search_query = ""
        self.client_filter: str | None = None
        self.client_label: str | None = None
        self.preview_enabled = False
        self._seen_ids: set[str] = set()
        self._detail_ticket: TicketRecord | None = None

    # ------------------------------------------------------------- lifecycle

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="counts")
        with Horizontal(id="body"):
            with Vertical(id="list-pane"):
                yield DataTable(id="tickets", cursor_type="row", zebra_stripes=True)
                yield Static("No tickets yet.", id="list-empty")
            yield TicketDetail(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tickets", DataTable)
        for key, label, width in TABLE_COLUMNS:
            table.add_column(label, key=key, width=width)
        table.focus()
        self.reload(announce=False, mark_read=True)
        self.call_after_refresh(self.fit_title_column)
        self.set_interval(self.POLL_SECONDS, self.poll)

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit_title_column)

    def fit_title_column(self) -> None:
        """Let the title soak up whatever width the other columns leave.

        Fixed widths alone would clip the reporter and age columns off the right
        edge on a narrow terminal.
        """
        table = self.query_one("#tickets", DataTable)
        column = table.columns.get(ColumnKey("title"))
        if column is None:
            return
        fixed = sum(width for key, _, width in TABLE_COLUMNS if key != "title")
        spare = table.size.width - fixed - table.cell_padding * 2 * len(TABLE_COLUMNS) - 1
        width = max(MIN_TITLE_WIDTH, spare)
        if width == column.width:
            return
        column.width = width
        column.content_width = width
        # Rows already on screen are cached at the old width; rebuilding the
        # table is what drops that cache.
        self.reload(announce=False)

    # ----------------------------------------------------------------- data

    def _build_filters(self) -> TicketFilters:
        return TicketFilters(
            client_id=self.client_filter,
            search=self.search_query or None,
            include_closed=self.filter_mode != "open",
            unread_only=self.filter_mode == "unread",
            limit=200,
        )

    def _fetch(self) -> list[TicketRecord]:
        tickets, _, _ = self.store.list_tickets(self._build_filters())
        return tickets

    def reload(self, *, announce: bool = True, mark_read: bool = False) -> None:
        """Repopulate the list, keeping the current selection where possible.

        A reload never marks the restored ticket read by default: otherwise the
        ten-second poll would silently undo `u`.
        """
        selected = self.selected_id
        try:
            tickets = self._fetch()
        except ServiceError as exc:
            self.notify(exc.message, severity="error")
            return

        arrived = [t for t in tickets if t.id not in self._seen_ids]
        self._seen_ids.update(t.id for t in tickets)
        self.tickets = tickets

        table = self.query_one("#tickets", DataTable)
        table.clear()
        for ticket in tickets:
            table.add_row(*self._row_cells(ticket), key=ticket.id)

        self.query_one("#list-empty", Static).display = not tickets
        table.display = bool(tickets)

        if tickets:
            index = next((i for i, t in enumerate(tickets) if t.id == selected), 0)
            table.move_cursor(row=index)
            self.select(tickets[index], mark_read=mark_read)
        else:
            self.select(None)

        self.update_counts()
        if announce and arrived:
            plural = "s" if len(arrived) > 1 else ""
            self.notify(f"{len(arrived)} new ticket{plural}", timeout=6)

    def poll(self) -> None:
        self.reload(announce=True)

    def _row_cells(self, ticket: TicketRecord) -> list[Text]:
        muted = ticket.status in MUTED_STATUSES
        base = "dim" if muted else ""
        priority_style = "dim" if muted else PRIORITY_STYLES.get(ticket.priority, "")
        return [
            Text("●" if ticket.unread else " ", style="dim" if muted else "bold cyan"),
            Text(ticket.ref, style=base or "bold"),
            Text(ticket.priority[:6], style=priority_style),
            Text(ticket.client_slug, style=base),
            Text(ticket.title, style=base, overflow="ellipsis", no_wrap=True),
            Text(ticket.reporter_name, style=base, overflow="ellipsis", no_wrap=True),
            Text(format_age(ticket.created_at), style=base or "dim"),
        ]

    def update_counts(self) -> None:
        counts = self.store.counts_by_status(self.client_filter)
        text = Text()
        text.append(f" {self.store.unread_count()} unread ", style="bold cyan reverse")
        for status in STATUS_ORDER:
            text.append(f"  {status} ", style="dim")
            text.append(str(counts.get(status, 0)), style="bold")
        self.query_one("#counts", Static).update(text)

        bits = [FILTER_LABELS[self.filter_mode]]
        if self.client_label:
            bits.append(self.client_label)
        if self.search_query:
            bits.append(f"search {self.search_query!r}")
        self.sub_title = " · ".join(bits)

    # ------------------------------------------------------------ selection

    @property
    def selected_id(self) -> str | None:
        return self._detail_ticket.id if self._detail_ticket else None

    @property
    def ticket(self) -> TicketRecord | None:
        return self._detail_ticket

    def select(self, ticket: TicketRecord | None, *, mark_read: bool = False) -> None:
        if ticket is None:
            self._detail_ticket = None
            self.call_next(self.render_detail)
            return
        if mark_read and ticket.unread:
            self.store.mark_read(ticket.id)
            ticket.unread = False
            self._refresh_row(ticket)
            self.update_counts()
        try:
            self._detail_ticket = self.store.get_ticket(ticket.id, include_internal=True)
        except ServiceError:
            self._detail_ticket = ticket
        self.call_next(self.render_detail)

    async def render_detail(self) -> None:
        detail = self.query_one("#detail", TicketDetail)
        await detail.show(self._detail_ticket, self._preview_renderable())

    def _preview_renderable(self) -> Text | str | None:
        ticket = self._detail_ticket
        if not self.preview_enabled or ticket is None:
            return None
        attachment = screenshot_of(ticket)
        if attachment is None:
            return "No image attachment to preview."
        try:
            path = self.store.attachment_path(attachment.id)
            width = max(20, min(self.query_one("#detail", TicketDetail).size.width - 2, 160))
            return render_image_preview(Path(path), width, PREVIEW_MAX_CELLS_HIGH)
        except Exception as exc:
            return f"Could not render {attachment.filename}: {exc}"

    @on(DataTable.RowHighlighted, "#tickets")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Rebuilding the table emits highlights of its own — an empty one from
        # `clear()`, then row 0 before the cursor is restored. Acting on those
        # would mark the wrong ticket read.
        if event.row_key is None or event.cursor_row != event.data_table.cursor_row:
            return
        key = event.row_key.value
        ticket = next((t for t in self.tickets if t.id == key), None)
        if ticket is None or ticket.id == self.selected_id:
            return
        self.select(ticket, mark_read=True)

    @on(DataTable.RowSelected, "#tickets")
    def _row_selected(self) -> None:
        self.action_focus_detail()

    def _refresh_row(self, ticket: TicketRecord) -> None:
        table = self.query_one("#tickets", DataTable)
        for (key, _label, _width), value in zip(TABLE_COLUMNS, self._row_cells(ticket)):
            table.update_cell(ticket.id, key, value)

    def _apply(self, changes: dict[str, Any]) -> None:
        ticket = self._detail_ticket
        if ticket is None:
            return
        try:
            self.store.update_ticket(ticket.id, changes, actor=self.operator)
        except ServiceError as exc:
            self.notify(exc.message, severity="error")
            return
        self.reload(announce=False)

    # -------------------------------------------------------------- actions

    def action_cursor_down(self) -> None:
        self.query_one("#tickets", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#tickets", DataTable).action_cursor_up()

    def action_goto_top(self) -> None:
        table = self.query_one("#tickets", DataTable)
        if table.row_count:
            table.move_cursor(row=0)

    def action_goto_bottom(self) -> None:
        table = self.query_one("#tickets", DataTable)
        if table.row_count:
            table.move_cursor(row=table.row_count - 1)

    def action_focus_detail(self) -> None:
        self.query_one("#detail", TicketDetail).focus()

    def action_refresh(self) -> None:
        self.reload(announce=False)
        self.notify("refreshed", timeout=2)

    def action_open_screenshot(self) -> None:
        ticket = self._detail_ticket
        if ticket is None:
            return
        attachment = screenshot_of(ticket)
        if attachment is None:
            self.notify("this ticket has no image attachment", severity="warning")
            return
        try:
            path = self.store.attachment_path(attachment.id)
            subprocess.Popen(
                [IMAGE_OPENER, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            # A missing viewer is an annoyance, never a reason to lose the inbox.
            self.notify(f"could not open {attachment.filename}: {exc}", severity="error")
            return
        self.notify(f"opened {attachment.filename}", timeout=3)

    async def action_toggle_preview(self) -> None:
        self.preview_enabled = not self.preview_enabled
        await self.render_detail()

    def action_mark_unread(self) -> None:
        ticket = self._detail_ticket
        if ticket is None:
            return
        self.store.mark_read(ticket.id, read=False)
        ticket.unread = True
        for row in self.tickets:
            if row.id == ticket.id:
                row.unread = True
                self._refresh_row(row)
        self.update_counts()

    def action_choose_status(self) -> None:
        ticket = self._detail_ticket
        if ticket is None:
            return
        self.push_screen(
            ChoiceScreen("Set status", [(s, s) for s in STATUS_ORDER], ticket.status),
            lambda value: self._apply({"status": value}) if value else None,
        )

    def action_choose_priority(self) -> None:
        ticket = self._detail_ticket
        if ticket is None:
            return
        self.push_screen(
            ChoiceScreen("Set priority", [(p, p) for p in PRIORITY_ORDER], ticket.priority),
            lambda value: self._apply({"priority": value}) if value else None,
        )

    def action_assign(self) -> None:
        ticket = self._detail_ticket
        if ticket is None:
            return
        def apply(value: str | None) -> None:
            # Escape gives None (cancel); an empty submission unassigns.
            if value is not None:
                self._apply({"assignee": value.strip()})

        self.push_screen(
            PromptScreen("Assign to", ticket.assignee or self.operator, "name, blank to clear"),
            apply,
        )

    def action_reply(self) -> None:
        self._compose("public")

    def action_note(self) -> None:
        self._compose("internal")

    def _compose(self, visibility: str) -> None:
        ticket = self._detail_ticket
        if ticket is None:
            return
        title = "Reply to client" if visibility == "public" else "Internal note"
        hint = (
            "ctrl+s to post, escape to discard — "
            + ("visible to the client" if visibility == "public" else "operator eyes only")
        )
        self.push_screen(
            ComposeScreen(title, hint),
            lambda body: self._post(ticket.id, body, visibility) if body else None,
        )

    def _post(self, ticket_id: str, body: str, visibility: str) -> None:
        try:
            self.store.add_comment(
                ticket_id,
                body=body,
                author_type="agent",
                author_name=self.operator,
                visibility=visibility,
            )
        except ServiceError as exc:
            self.notify(exc.message, severity="error")
            return
        self.reload(announce=False)
        self.notify("note added" if visibility == "internal" else "reply posted", timeout=3)

    def action_search(self) -> None:
        def apply(value: str | None) -> None:
            if value is None:
                return
            self.search_query = value.strip()
            self.reload(announce=False)

        prompt = PromptScreen("Search", self.search_query, "title, ref or reporter")
        self.push_screen(prompt, apply)

    def action_cycle_filter(self) -> None:
        index = FILTER_MODES.index(self.filter_mode)
        self.filter_mode = FILTER_MODES[(index + 1) % len(FILTER_MODES)]
        self.reload(announce=False)
        self.notify(FILTER_LABELS[self.filter_mode], timeout=2)

    def action_choose_client(self) -> None:
        clients = self.store.list_clients()
        choices = [("", "All clients")] + [(c.id, f"{c.name} ({c.slug})") for c in clients]

        def apply(value: str | None) -> None:
            if value is None:
                return
            self.client_filter = value or None
            self.client_label = next(
                (label for cid, label in choices if cid == value and value), None
            )
            self.reload(announce=False)

        chooser = ChoiceScreen("Filter by client", choices, self.client_filter or "")
        self.push_screen(chooser, apply)


def run() -> None:
    """Entry point used by `otk tui`."""
    TicketApp(Store()).run()
