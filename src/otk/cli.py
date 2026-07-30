"""Operator command line: manage clients and keys, run the server, open the TUI."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from .auth import WeakPassword
from .config import get_settings
from .schemas import TicketStatus
from .service import ServiceError, Store, iso, now

console = Console()

app = typer.Typer(
    help="Odoo ticket intake: intake API, operator web UI and TUI, and admin.",
    no_args_is_help=True,
    add_completion=False,
)
client_app = typer.Typer(help="Manage clients.", no_args_is_help=True)
key_app = typer.Typer(help="Manage per-client API keys.", no_args_is_help=True)
operator_app = typer.Typer(help="Manage web-UI operator accounts.", no_args_is_help=True)
app.add_typer(client_app, name="client")
app.add_typer(key_app, name="key")
app.add_typer(operator_app, name="operator")


def _store(announce: bool = False) -> Store:
    """Open the store, optionally saying which database it opened.

    Worth the noise on any command that writes: the data directory comes from
    OTK_DATA_DIR, which systemd supplies from its EnvironmentFile but a plain
    shell does not. Without this, `sudo -u otk otk operator add` silently
    creates the account in a second database under $HOME and the service keeps
    insisting no operator exists.
    """
    settings = get_settings()
    if announce:
        console.print(f"[dim]data dir: {settings.data_dir}[/dim]")
    return Store(settings)


def _fail(exc: ServiceError) -> None:
    console.print(f"[red]error[/red] ({exc.code}) {exc.message}")
    raise typer.Exit(1)


# ------------------------------------------------------------------- clients


@client_app.command("add")
def client_add(
    name: Annotated[str, typer.Argument(help="Display name, e.g. 'Acme Industries'")],
    slug: Annotated[
        Optional[str], typer.Option(help="Short id used in ticket refs; defaults from name")
    ] = None,
    email: Annotated[Optional[str], typer.Option(help="Main contact address")] = None,
    odoo_url: Annotated[Optional[str], typer.Option(help="Their Odoo base URL")] = None,
    with_key: Annotated[bool, typer.Option(help="Also issue a first API key")] = True,
) -> None:
    """Register a client and, by default, issue its first API key."""
    store = _store(announce=True)
    resolved_slug = slug or "".join(
        ch if ch.isalnum() else "-" for ch in name.lower()
    ).strip("-").replace("--", "-")
    try:
        client = store.create_client(
            name=name, slug=resolved_slug, contact_email=email, odoo_url=odoo_url
        )
    except ServiceError as exc:
        _fail(exc)

    console.print(f"[green]created[/green] client [bold]{client.name}[/bold] ({client.slug})")
    console.print(f"  id: {client.id}")
    if with_key:
        key_id, token = store.issue_api_key(client.id, name="default")
        _print_new_key(key_id, token)


def _print_new_key(key_id: str, token: str) -> None:
    console.print()
    console.print(f"  key id: [bold]{key_id}[/bold]")
    console.print(f"  token:  [bold yellow]{token}[/bold yellow]")
    console.print(
        "  [dim]Shown once — it is stored only as a hash. Put it in the client's Odoo\n"
        "  under System Parameters as `odoo_tickets.api_key`, never in browser JS.[/dim]"
    )


@client_app.command("list")
def client_list() -> None:
    """List clients with their open ticket counts."""
    store = _store()
    table = Table(title="Clients")
    for column in ("slug", "name", "contact", "tickets", "open", "active"):
        table.add_column(column)
    for client in store.list_clients():
        counts = store.counts_by_status(client.id)
        total = sum(counts.values())
        open_count = total - counts.get("closed", 0) - counts.get("resolved", 0)
        table.add_row(
            client.slug,
            client.name,
            client.contact_email or "-",
            str(total),
            str(open_count),
            "yes" if client.active else "[red]no[/red]",
        )
    console.print(table)


@client_app.command("disable")
def client_disable(slug: str) -> None:
    """Block a client's keys without deleting their history."""
    store = _store()
    try:
        client = store.get_client_by_slug(slug)
    except ServiceError as exc:
        _fail(exc)
    store.set_client_active(client.id, False)
    console.print(f"[yellow]disabled[/yellow] {client.name}")


@client_app.command("enable")
def client_enable(slug: str) -> None:
    """Re-enable a disabled client."""
    store = _store()
    try:
        client = store.get_client_by_slug(slug)
    except ServiceError as exc:
        _fail(exc)
    store.set_client_active(client.id, True)
    console.print(f"[green]enabled[/green] {client.name}")


# ---------------------------------------------------------------------- keys


@key_app.command("issue")
def key_issue(
    slug: Annotated[str, typer.Argument(help="Client slug")],
    name: Annotated[str, typer.Option(help="Label, e.g. 'prod' or 'staging'")] = "default",
    expires_days: Annotated[Optional[int], typer.Option(help="Expiry in days")] = None,
) -> None:
    """Issue a new API key for a client. Printed once, then unrecoverable."""
    store = _store(announce=True)
    try:
        client = store.get_client_by_slug(slug)
        expires = now() + timedelta(days=expires_days) if expires_days else None
        key_id, token = store.issue_api_key(client.id, name=name, expires_at=expires)
    except ServiceError as exc:
        _fail(exc)
    console.print(f"issued key for [bold]{client.name}[/bold]")
    _print_new_key(key_id, token)


@key_app.command("list")
def key_list(
    slug: Annotated[Optional[str], typer.Option(help="Filter to one client")] = None,
) -> None:
    """List API keys. Secrets are never shown."""
    store = _store()
    client_id = None
    if slug:
        try:
            client_id = store.get_client_by_slug(slug).id
        except ServiceError as exc:
            _fail(exc)
    table = Table(title="API keys")
    for column in ("key id", "client", "name", "created", "last used", "state"):
        table.add_column(column)
    for row in store.list_api_keys(client_id):
        if row["revoked_at"]:
            state = "[red]revoked[/red]"
        elif row["expires_at"] and row["expires_at"] < iso(now()):
            state = "[yellow]expired[/yellow]"
        else:
            state = "[green]active[/green]"
        table.add_row(
            row["id"],
            row["client_slug"],
            row["name"],
            (row["created_at"] or "")[:10],
            (row["last_used_at"] or "never")[:16],
            state,
        )
    console.print(table)


@key_app.command("revoke")
def key_revoke(key_id: Annotated[str, typer.Argument(help="Key id from `otk key list`")]) -> None:
    """Revoke a key immediately."""
    store = _store()
    try:
        store.revoke_api_key(key_id)
    except ServiceError as exc:
        _fail(exc)
    console.print(f"[red]revoked[/red] {key_id}")


# -------------------------------------------------------------------- server


@app.command()
def serve(
    host: Annotated[Optional[str], typer.Option(help="Bind address")] = None,
    port: Annotated[Optional[int], typer.Option(help="Bind port")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes")] = False,
) -> None:
    """Run the intake API."""
    import uvicorn

    settings = get_settings()
    console.print(f"[dim]data dir: {settings.data_dir}[/dim]")
    uvicorn.run(
        "otk.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


@app.command()
def web(
    host: Annotated[Optional[str], typer.Option(help="Bind address")] = None,
    port: Annotated[Optional[int], typer.Option(help="Bind port")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes")] = False,
) -> None:
    """Run the operator web interface."""
    import uvicorn

    settings = get_settings()
    if not _store().has_operators():
        # Warn, but still serve. Exiting here means a service manager sees a
        # crash loop for what is really a setup step, and the login page
        # already tells you which command to run.
        console.print(
            "[yellow]No operator account yet — nobody can sign in.[/yellow]\n"
            "  otk operator add <username>"
        )
    uvicorn.run(
        "otk.web.app:create_web_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.web_port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


@app.command()
def tui() -> None:
    """Open the ticket inbox in the terminal."""
    from .tui.app import run

    run()


# ------------------------------------------------------------------ operators


@operator_app.command("add")
def operator_add(
    username: Annotated[str, typer.Argument(help="Login name")],
    display_name: Annotated[Optional[str], typer.Option(help="Name shown on replies")] = None,
) -> None:
    """Create a web-UI operator account, prompting for the password."""
    store = _store(announce=True)
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        store.create_operator(username, password, display_name or username)
    except ServiceError as exc:
        _fail(exc)
    except WeakPassword as exc:
        console.print(f"[red]error[/red] {exc}")
        raise typer.Exit(1)
    console.print(f"[green]created[/green] operator [bold]{username}[/bold]")


@operator_app.command("passwd")
def operator_passwd(username: Annotated[str, typer.Argument(help="Login name")]) -> None:
    """Change an operator's password. Signs out all their sessions."""
    store = _store(announce=True)
    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    try:
        store.set_operator_password(username, password)
    except ServiceError as exc:
        _fail(exc)
    except WeakPassword as exc:
        console.print(f"[red]error[/red] {exc}")
        raise typer.Exit(1)
    console.print(f"[green]updated[/green] password for {username}; all sessions signed out")


@operator_app.command("list")
def operator_list() -> None:
    """List operator accounts."""
    store = _store()
    table = Table(title="Operators")
    for column in ("username", "display name", "created", "last login"):
        table.add_column(column)
    for row in store.list_operators():
        table.add_row(
            row["username"],
            row["display_name"],
            (row["created_at"] or "")[:10],
            (row["last_login_at"] or "never")[:16],
        )
    console.print(table)


@operator_app.command("check")
def operator_check(
    username: Annotated[str, typer.Argument(help="Login name")],
) -> None:
    """Test a password against the database, without a browser.

    Separates the two reasons a login fails — wrong password, or the web
    service reading a different database than the one you edited.
    """
    store = _store(announce=True)
    names = [row["username"] for row in store.list_operators()]
    console.print(f"operators in this database: {', '.join(names) or '[red]none[/red]'}")
    if username.strip().lower() not in names:
        console.print(
            f"[red]{username!r} is not in this database.[/red] The web service may be "
            "reading a different one — compare with:\n"
            "  tr '\\0' '\\n' < /proc/$(systemctl show -p MainPID --value otk-web)/environ"
            " | grep OTK_DATA_DIR"
        )
        raise typer.Exit(1)

    password = typer.prompt("Password", hide_input=True)
    try:
        store.login(username, password, user_agent="otk operator check")
    except ServiceError as exc:
        console.print(f"[red]rejected[/red] — {exc.message}")
        raise typer.Exit(1)
    console.print("[green]accepted[/green] — these credentials work on this database")


@operator_app.command("remove")
def operator_remove(
    username: Annotated[str, typer.Argument(help="Login name")],
    force: Annotated[
        bool, typer.Option("--force", help="Allow removing the last operator")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt")] = False,
) -> None:
    """Delete an operator account and sign out all its sessions.

    Replies they already wrote stay on their tickets.
    """
    store = _store(announce=True)
    if not yes and not typer.confirm(f"Remove operator {username!r}?", default=False):
        console.print("[dim]Aborted.[/dim]")
        return
    try:
        store.delete_operator(username, force=force)
    except ServiceError as exc:
        _fail(exc)
    console.print(f"[red]removed[/red] operator {username}")


@operator_app.command("logout-all")
def operator_logout_all(username: Annotated[str, typer.Argument(help="Login name")]) -> None:
    """Revoke every active session for an operator."""
    count = _store().revoke_operator_sessions(username)
    console.print(f"[yellow]revoked[/yellow] {count} session(s)")


# ------------------------------------------------------------------ storage


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


@app.command()
def purge(
    older_than: Annotated[int, typer.Option(help="Age in days, measured from last update")] = 90,
    scope: Annotated[str, typer.Option(help="'closed' or 'all'")] = "closed",
    client: Annotated[Optional[str], typer.Option(help="Limit to one client slug")] = None,
    auto: Annotated[
        bool, typer.Option(help="Use OTK_RETENTION_DAYS; no-op when it is 0. For cron.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt")] = False,
) -> None:
    """Strip attachments from old tickets, keeping the ticket text.

    Shows exactly what would go and waits for confirmation unless --yes.
    """
    store = _store()
    if auto:
        if store.settings.retention_days <= 0:
            console.print("[dim]OTK_RETENTION_DAYS is 0; automatic retention disabled.[/dim]")
            return
        older_than = store.settings.retention_days

    statuses = (
        [TicketStatus.CLOSED, TicketStatus.RESOLVED] if scope == "closed" else list(TicketStatus)
    )
    client_id = None
    if client:
        try:
            client_id = store.get_client_by_slug(client).id
        except ServiceError as exc:
            _fail(exc)

    candidates = store.purge_candidates(
        older_than_days=older_than, statuses=statuses, client_id=client_id
    )
    if not candidates:
        console.print("[dim]Nothing to purge.[/dim]")
        return

    total_bytes = sum(c["bytes"] for c in candidates)
    total_files = sum(c["file_count"] for c in candidates)

    table = Table(title=f"Would purge files from {len(candidates)} ticket(s)")
    for column in ("ref", "client", "status", "last update", "files", "size"):
        table.add_column(column)
    for row in candidates[:40]:
        table.add_row(
            row["ref"],
            row["client_slug"],
            row["status"],
            (row["updated_at"] or "")[:10],
            str(row["file_count"]),
            _human(row["bytes"]),
        )
    console.print(table)
    if len(candidates) > 40:
        console.print(f"[dim]… and {len(candidates) - 40} more[/dim]")
    console.print(
        f"\n[bold]{total_files} file(s), {_human(total_bytes)}[/bold] would be deleted."
        "\nTicket text, context and comments are [bold]kept[/bold]."
    )

    if not yes and not typer.confirm("Delete these files permanently?", default=False):
        console.print("[dim]Aborted.[/dim]")
        return

    result = store.purge_attachments([c["id"] for c in candidates])
    console.print(
        f"[green]purged[/green] {result['files']} file(s) from {result['tickets']} ticket(s),"
        f" freeing {_human(result['bytes_freed'])}"
    )


@app.command()
def gc(
    yes: Annotated[bool, typer.Option("--yes", help="Delete without confirming")] = False,
) -> None:
    """Delete blobs no ticket references (left by rolled-back uploads)."""
    store = _store()
    found = store.collect_orphan_blobs(dry_run=True)
    if not found["blobs"]:
        console.print("[dim]No orphan blobs.[/dim]")
        return
    console.print(f"{found['blobs']} orphan blob(s), {_human(found['bytes'])}")
    if not yes and not typer.confirm("Delete them?", default=False):
        return
    removed = store.collect_orphan_blobs(dry_run=False)
    console.print(f"[green]removed[/green] {removed['blobs']}, freed {_human(removed['bytes'])}")


@app.command()
def usage() -> None:
    """Show what is taking up disk."""
    store = _store()
    stats = store.storage_usage()
    orphans = store.collect_orphan_blobs(dry_run=True)
    console.print(f"attachments:  {stats['attachment_rows']}")
    console.print(f"logical size: {_human(stats['logical_bytes'])}  [dim](before dedup)[/dim]")
    console.print(f"on disk:      {_human(stats['disk_bytes'])}")
    console.print(f"orphans:      {orphans['blobs']} ({_human(orphans['bytes'])})")


@app.command()
def openapi(
    out: Annotated[Optional[Path], typer.Option(help="Write to file instead of stdout")] = None,
) -> None:
    """Export the OpenAPI document to hand to the Odoo module implementer."""
    from .api.app import create_app

    spec = create_app().openapi()
    text = json.dumps(spec, indent=2)
    if out is None:
        console.print_json(text)
        return
    if out.suffix in (".yaml", ".yml"):
        import yaml

        text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
    out.write_text(text, encoding="utf-8")
    console.print(f"wrote {out}")


# Fields whose absence actually costs a round trip with the client. Everything
# else in TicketContext is a nice-to-have.
CRITICAL_CONTEXT = ("url", "odoo_version", "database", "model", "res_id", "view_type")


@app.command()
def inspect(
    ref: Annotated[
        Optional[str], typer.Argument(help="Ticket ref or id; defaults to the newest")
    ] = None,
    last: Annotated[int, typer.Option(help="Inspect the N most recent tickets")] = 1,
) -> None:
    """Report which context fields a ticket actually arrived with.

    A module can run cleanly and still send almost nothing — the property paths
    it reads move between Odoo versions, and a missing field looks identical to
    a field that was never there. This shows exactly what landed.
    """
    from .schemas import TicketContext
    from .service import TicketFilters

    store = _store()
    if ref:
        try:
            tickets = [store.get_ticket(ref)]
        except ServiceError as exc:
            _fail(exc)
    else:
        tickets, _, _ = store.list_tickets(TicketFilters(limit=max(1, last)))
    if not tickets:
        console.print("[dim]No tickets yet. File one from the module, then re-run this.[/dim]")
        return

    documented = list(TicketContext.model_fields)
    for ticket in tickets:
        ctx = ticket.context or {}
        console.print(
            f"\n[bold]{ticket.ref}[/bold]  {ticket.title[:60]}"
            f"  [dim]{ticket.source} · {ticket.client_slug}[/dim]"
        )

        table = Table(show_header=True, header_style="dim")
        table.add_column("context field")
        table.add_column("value")
        missing_critical = []
        for name in documented:
            if name in ("error", "browser"):
                continue
            value = ctx.get(name)
            present = value not in (None, "", [], {})
            if not present and name in CRITICAL_CONTEXT:
                missing_critical.append(name)
            mark = "[green]✓[/green]" if present else "[red]✗[/red]"
            shown = "[dim]—[/dim]" if not present else str(value)[:60]
            table.add_row(f"{mark} {name}", shown)
        console.print(table)

        for block in ("error", "browser"):
            sub = ctx.get(block)
            if isinstance(sub, dict) and sub:
                filled = ", ".join(k for k, v in sub.items() if v not in (None, ""))
                console.print(f"  [green]✓[/green] {block}: {filled or '[dim]empty[/dim]'}")
            else:
                console.print(f"  [dim]· {block}: not sent[/dim]")

        extra = [k for k in ctx if k not in documented]
        if extra:
            console.print(f"  [cyan]+[/cyan] extra keys kept: {', '.join(extra)}")

        shots = [a for a in ticket.attachments if a.role == "screenshot"]
        others = [a for a in ticket.attachments if a.role != "screenshot"]
        console.print(
            f"  {'[green]✓[/green]' if shots else '[red]✗[/red]'} screenshot: "
            + (
                f"{shots[0].filename} {shots[0].width}x{shots[0].height}"
                f" ({_human(shots[0].size_bytes)})"
                if shots
                else "[red]none sent[/red]"
            )
            + (f"   +{len(others)} other file(s)" if others else "")
        )
        if not ticket.reporter_email and not ticket.reporter.get("odoo_uid"):
            console.print(
                "  [yellow]![/yellow] reporter has no email and no odoo_uid —"
                " the same person will not deduplicate across tickets"
            )
        if missing_critical:
            console.print(
                f"  [yellow]![/yellow] missing high-value fields: "
                f"[bold]{', '.join(missing_critical)}[/bold]"
            )


@app.command()
def seed() -> None:
    """Create a demo client with sample tickets, for trying out the TUI."""
    from .demo import seed_demo_data

    seed_demo_data(_store())
    console.print("[green]seeded[/green] demo data — run `otk tui` to view it")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
