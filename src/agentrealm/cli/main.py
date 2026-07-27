"""Entry point for the `arealm` CLI (M7)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from pydantic import ValidationError

from agentrealm.core import PackageError, load_package
from agentrealm.core.jsonschema import write_schemas
from agentrealm.core.plugins import load_command_plugins
from agentrealm.scribe.backend import DEFAULT_MODEL

if TYPE_CHECKING:
    from agentrealm.chronicle import Chronicle
    from agentrealm.core.schema import Project

app = typer.Typer(
    name="arealm",
    help="AgentRealm — run realms of autonomous AI agents that collaborate or compete.",
    no_args_is_help=True,
)


@app.callback()
def callback() -> None:
    """AgentRealm command-line interface."""


@app.command()
def reap(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="list what would be destroyed, and do nothing"
    ),
    force: bool = typer.Option(
        False, "--force",
        help="destroy agent containers even for realms the control plane says are RUNNING",
    ),
    api: str = typer.Option(
        "http://127.0.0.1:8000", "--api", envvar="AGENTREALM_API",
        help="control plane to ask which realms are live.",
    ),
) -> None:
    """Destroy agent containers left behind by a crashed or killed platform.

    Teardown only runs when a realm CONCLUDES. If the platform dies — a crash, a kill -9, a reboot —
    nothing ever stops the agents, and they keep running with a live model key and nobody watching.
    `serve` sweeps at startup and shutdown; this is the same sweep, on demand.

    It asks the control plane which realms are live first and SPARES those. If the control plane is
    not answering there is nothing to spare: the platform is the only thing that can run a realm, so
    anything alive while it is down is by definition an orphan.

    Use --force to destroy a live realm's agents anyway. That kills the run mid-flight and the
    chronicle never gets its conclusion, so it is deliberately not the default.
    """
    import httpx

    from agentrealm.forge import DockerRuntime, orphan_containers

    runtime = DockerRuntime(os.environ.get("DOCKER_HOST") or None)
    found = runtime.list_containers("realm-")
    if not found:
        typer.echo("no agent containers running — nothing to reap")
        raise typer.Exit()

    active: list[str] = []
    if not force:
        try:
            from agentrealm.gatekeeper.auth import load_or_create_token

            r = httpx.get(
                f"{api.rstrip('/')}/api/realms", timeout=2.0,
                headers={"Authorization": f"Bearer {load_or_create_token()}"},
            )
            r.raise_for_status()
            active = [
                str(x.get("realm_id") or x.get("id"))
                for x in (r.json().get("realms") or [])
                if x.get("state") == "running"
            ]
        except Exception:  # noqa: BLE001 - no control plane answering means nothing is live
            active = []

    orphans = orphan_containers(found, active)
    spared = len(found) - len(orphans)
    if spared:
        typer.secho(
            f"↷ sparing {spared} container(s) belonging to {len(active)} running realm(s) "
            f"({', '.join(sorted(active))}) — pass --force to destroy them too",
            fg=typer.colors.YELLOW,
        )
    if not orphans:
        typer.echo("no orphaned agent containers — nothing to reap")
        raise typer.Exit()
    if dry_run:
        typer.echo(f"would destroy {len(orphans)} container(s):")
        for name in sorted(orphans):
            typer.echo(f"  {name}")
        raise typer.Exit()
    for name, cid in sorted(orphans.items()):
        with contextlib.suppress(Exception):
            runtime.stop_container(cid, timeout=5)
        with contextlib.suppress(Exception):
            runtime.remove_container(cid)
        typer.echo(f"destroyed {name}")
    keep_nets = tuple(f"realm-{r}" for r in active)
    for net in runtime.list_networks("realm-"):
        if net in keep_nets:
            continue
        with contextlib.suppress(Exception):
            runtime.remove_network(net)
    typer.echo(f"reaped {len(orphans)} orphaned agent container(s)")


@app.command()
def version() -> None:
    """Print the AgentRealm version."""
    from importlib.metadata import version as pkg_version

    typer.echo(f"agentrealm {pkg_version('agentrealm')}")


@app.command()
def validate(
    path: str = typer.Argument(..., help="Project package folder or project.json"),
) -> None:
    """Validate a project package (or flat manifest) and print a summary."""
    try:
        project = load_package(path)
    except (PackageError, ValidationError) as exc:
        typer.secho(f"✗ invalid project: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    ref = project.referee
    typer.secho(f"✓ {project.metadata.name} — valid", fg=typer.colors.GREEN)
    typer.echo(f"  agents:   {len(project.agents)} ({', '.join(a.id for a in project.agents)})")
    typer.echo(f"  referee:  {ref.id if ref else '—'}")
    typer.echo(f"  mechanics: {', '.join(m.kind for m in project.spec.mechanics) or '—'}")
    typer.echo(f"  termination: {', '.join(t.type for t in project.spec.termination) or '—'}")
    persistent = [a.id for a in project.agents if a.memory != a.memory.EPHEMERAL]
    if persistent:
        typer.secho(
            f"  note: persistent memory ({', '.join(persistent)}) is a v3 feature — MVP ignores it",
            fg=typer.colors.YELLOW,
        )


@app.command()
def schema(
    out: str = typer.Option("schemas", "--out", "-o", help="Directory to write JSON Schemas into"),
) -> None:
    """Export JSON Schemas (project + agent) for editor/CI validation."""
    for path in write_schemas(out):
        typer.echo(f"wrote {Path(path)}")


@app.command()
def up(
    path: str = typer.Argument(..., help="Project package folder or project.json"),
    realm_id: str = typer.Option(None, "--realm", help="Realm id (default: derived from name)"),
    free_response: bool = typer.Option(
        False, "--free-response", help="Free-response rooms (default: mention-gated)"
    ),
) -> None:
    """Provision and run a realm to conclusion, then print the final report.

    Requires the platform stack up (deploy/docker-compose.yaml) and the pinned Hermes image.
    """
    project = _load_or_exit(path)
    # a fresh id per run by default — realm-scoped Matrix users can't be re-created, so reusing
    # an id collides. Pass --realm to pin one deliberately.
    rid = realm_id or f"{_slug(project.metadata.name)}-{secrets.token_hex(3)}"
    typer.secho(f"↑ running realm {rid!r} from {project.metadata.name!r}…", fg=typer.colors.CYAN)
    report = asyncio.run(_run_realm(rid, project, require_mention=not free_response))
    typer.echo("\n" + report)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the HTTP API + dashboard (control panel) at http://host:port/.

    Requires the platform stack up and the same env as `up` (LITELLM_MASTER_KEY,
    AGENTREALM_KEYSTORE_KEY, AGENTREALM_SYSTEM_PASSWORD, REALMTOOLS_SECRET).
    """
    import uvicorn

    from agentrealm.gatekeeper.auth import load_or_create_token, token_path

    token = load_or_create_token()
    typer.secho(f"↑ AgentRealm control panel → http://{host}:{port}/?token={token}",
                fg=typer.colors.CYAN)
    typer.secho(
        "  Open that URL once — it stores a cookie, and the plain address works afterwards.\n"
        f"  Scripts: Authorization: Bearer <token>   (token stored in {token_path()})",
        fg=typer.colors.BRIGHT_BLACK,
    )
    uvicorn.run("agentrealm.gatekeeper.api:app", host=host, port=port)


@app.command()
def assist(
    api_base: str = typer.Option(
        "http://127.0.0.1:4000/v1", "--api-base", envvar="SCRIBE_API_BASE",
        help="OpenAI-compatible base URL for the model endpoint (include /v1)."),
    api_key: str = typer.Option(
        "", "--api-key", envvar="SCRIBE_API_KEY",
        help="Bearer token for that endpoint. Omit for one that needs none."),
    root: str = typer.Option(
        None, "--root", envvar="SCRIBE_DATA_ROOT",
        help="Scribe data root (memory + versions + authored scenarios). "
        "Default: ~/.agentrealm/scribe."),
    model: str = typer.Option(
        DEFAULT_MODEL, "--model", envvar="SCRIBE_MODEL",
        help="Model to author with. Must be one the endpoint serves."),
) -> None:
    """Chat with Scribe to author scenarios: describe one in plain language and Scribe creates,
    edits, and validates the package for you. Point --api-base at any OpenAI-compatible endpoint.

    Type your request at the prompt; 'exit' or Ctrl-D to leave. Every write is validated first and
    snapshotted (revertible). Scribe never touches secrets or the host — only scenario files.
    """
    from agentrealm.scribe.service import ScribeSession, build_scribe

    data_root = Path(root) if root else Path.home() / ".agentrealm" / "scribe"
    loop = build_scribe(api_base, data_root, api_key=api_key, model=model)
    session = ScribeSession(loop)
    typer.secho(
        f"↑ Scribe → {api_base} ({model})  (data: {data_root})", fg=typer.colors.CYAN)
    typer.secho("Describe a scenario, or ask Scribe to edit one. 'exit' to quit.\n",
                fg=typer.colors.BRIGHT_BLACK)
    asyncio.run(_assist_repl(session))


@app.command()
def status(realm_id: str = typer.Argument(...)) -> None:
    """Show a realm's current state, agent spend, and last events from the Chronicle."""
    asyncio.run(_status(realm_id))


@app.command()
def tail(
    realm_id: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Print the tail of a realm's message transcript."""
    asyncio.run(_tail(realm_id, limit))


@app.command()
def trace(
    path: str = typer.Argument("", help="Telemetry JSONL (default: $AGENTREALM_TELEMETRY)"),
    realm: str = typer.Option(
        "", "--realm", "-r", help="Only spans from this realm id (first-class attribute; "
        "falls back to a text match for spans captured before realm-tagging)."),
    agent: str = typer.Option(
        "", "--agent", "-a", help="Only spans from this agent id (combine with --realm)."),
    grep: str = typer.Option(
        "", "--grep", "-g", help="Only spans whose system prompt / completion contains this text "
        "(e.g. an agent's name or a rubric phrase, to isolate one agent's calls)."),
    tool: str = typer.Option(
        "", "--tool", help="Report whether THIS tool was offered to and called by the model."),
    last: int = typer.Option(15, "--last", "-n", help="Show the last N matching spans."),
    full: bool = typer.Option(False, "--full", help="Print the full system prompt + completion."),
) -> None:
    """Inspect captured LLM I/O: exactly what each agent's model received (system prompt + tools)
    and produced (completion + tool calls). Enable capture by pointing AGENTREALM_TELEMETRY at a
    file before starting whatever component sits at the LLM chokepoint."""
    import json

    src = path or os.environ.get("AGENTREALM_TELEMETRY") or os.environ.get("AGENTREALM_LLM_TRACE")
    if not src or not Path(src).exists():
        typer.secho("No telemetry file. Set AGENTREALM_TELEMETRY=<file>, then run a "
                    "realm.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    spans = [json.loads(ln) for ln in Path(src).read_text().splitlines() if ln.strip()]

    def _text(a: dict[str, Any]) -> str:
        parts = (a.get("agentrealm.request.system_prompt", ""),
                 a.get("agentrealm.request.prompt", ""),
                 a.get("agentrealm.response.completion", ""))
        return " ".join(str(p) for p in parts).lower()

    if realm:
        spans = [s for s in spans
                 if s.get("attributes", {}).get("agentrealm.realm.id") == realm
                 or (not s.get("attributes", {}).get("agentrealm.realm.id")
                     and realm.lower() in _text(s.get("attributes", {})))]
    if agent:
        spans = [s for s in spans
                 if s.get("attributes", {}).get("agentrealm.agent.id") == agent]
    if grep:
        spans = [s for s in spans if grep.lower() in _text(s.get("attributes", {}))]
    shown = spans[-last:]
    offered_ct = called_ct = 0
    for s in shown:
        a = s.get("attributes", {})
        model = a.get("agentrealm.request.model_alias") or a.get("gen_ai.request.model", "?")
        offered = a.get("agentrealm.request.tool_names", [])
        called = a.get("agentrealm.response.tool_calls", [])
        sysp = " ".join(a.get("agentrealm.request.system_prompt", "").split())
        comp = " ".join(a.get("agentrealm.response.completion", "").split())
        err = a.get("agentrealm.error.message")
        tin = a.get("gen_ai.usage.input_tokens")
        tout = a.get("gen_ai.usage.output_tokens")
        typer.secho(f"── {model}  ({tin}→{tout} tok, {s.get('duration_ms', '?')}ms)",
                    fg=typer.colors.CYAN)
        if tool:
            off = tool in offered
            cal = tool in called
            offered_ct += off
            called_ct += cal
            typer.secho(
                f"   {tool!r}: {'OFFERED' if off else 'NOT offered'}, "
                f"{'CALLED' if cal else 'not called'}",
                fg=(typer.colors.GREEN if cal else typer.colors.YELLOW if off
                    else typer.colors.RED))
        else:
            typer.echo(f"   tools offered ({len(offered)}): {', '.join(offered) or '—'}")
            typer.echo(f"   tools called: {', '.join(called) or '—'}")
        if err:
            typer.secho(f"   ERROR: {err[:200]}", fg=typer.colors.RED)
        typer.echo(f"   system: {sysp if full else sysp[:220] + ('…' if len(sysp) > 220 else '')}")
        typer.echo(f"   output: {comp if full else comp[:220] + ('…' if len(comp) > 220 else '')}")
    dim = typer.colors.BRIGHT_BLACK
    typer.secho(f"\n{len(shown)} span(s) shown ({len(spans)} matched).", fg=dim)
    if tool:
        typer.secho(f"{tool!r}: offered in {offered_ct}, called in {called_ct}.", fg=dim)


@app.command()
def archive(
    realm_id: str = typer.Argument(...),
    out: str = typer.Option(".", "--out", "-o", help="Directory to write the archive into"),
) -> None:
    """Write a realm's transcript + final report to disk."""
    asyncio.run(_archive(realm_id, out))


@app.command()
def stop(realm_id: str = typer.Argument(...)) -> None:
    """Signal a running realm's kill switch (drops a stop flag the Runner watches)."""
    _stop_flag_path(realm_id).write_text("stop")
    typer.secho(f"kill switch set for realm {realm_id!r}", fg=typer.colors.YELLOW)


@app.command()
def msg(
    realm_id: str = typer.Argument(...),
    text: str = typer.Argument(..., help="Message to inject into the commons as the operator"),
) -> None:
    """Inject a message into a realm's commons (influence-by-message; never mid-run control)."""
    asyncio.run(_inject(realm_id, text))


keys_app = typer.Typer(help="Manage BYOK provider credentials (encrypted keystore).")
app.add_typer(keys_app, name="keys")


@keys_app.command("add")
def keys_add(
    handle: str = typer.Argument(
        ..., help="Handle agents reference via api_key_ref, e.g. 'azure-main'."
    ),
    api_base: str = typer.Option(
        None, "--api-base", help="Provider endpoint base URL, e.g. https://…/openai/v1"
    ),
    provider: str = typer.Option(None, "--provider", help="Provider tag (azure/openai/…)."),
    api_key: str = typer.Option(
        None, "--api-key", help="API key. OMIT to be prompted securely (recommended)."
    ),
) -> None:
    """Store (or replace) a provider credential. Supply the key WITHOUT pasting into a hidden
    prompt, via any of (priority order): --api-key, piped stdin (e.g. `pbpaste | arealm keys add
    …`), the AGENTREALM_API_KEY env var. Falls back to a hidden prompt only on a bare tty. An
    explicit pipe wins over the env var, so a stale AGENTREALM_API_KEY can't silently shadow it.
    Encrypted at rest with AGENTREALM_KEYSTORE_KEY; never written to source or logs."""
    import os
    import sys

    from agentrealm.gatekeeper.service import ConfigError, open_keystore

    if not api_key and not sys.stdin.isatty():
        api_key = sys.stdin.readline().strip()  # piped input wins over an ambient env var
    if not api_key:
        api_key = os.environ.get("AGENTREALM_API_KEY", "")
    if not api_key:
        api_key = typer.prompt("API key", hide_input=True)
    if not api_key.strip():
        typer.secho("no API key supplied", fg="red", err=True)
        raise typer.Exit(1)
    try:
        ks = open_keystore()
    except ConfigError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    ks.put(handle, api_key=api_key, api_base=api_base, provider=provider)
    dest = f" → {api_base}" if api_base else ""
    typer.secho(f"stored credential {handle!r}{dest}", fg="green")


@keys_app.command("list")
def keys_list() -> None:
    """List credential handles (never prints the keys themselves)."""
    from agentrealm.gatekeeper.service import ConfigError, open_keystore

    try:
        ks = open_keystore()
    except ConfigError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    handles = ks.handles()
    for h in handles:
        cred = ks.get(h)
        typer.echo(f"{h}\t{cred.api_base or '(no api_base)'}\t{cred.provider or ''}")
    if not handles:
        typer.echo("(no credentials — add one with: arealm keys add <handle> --api-base <url>)")


# --- helpers ----------------------------------------------------------------
def _load_or_exit(path: str) -> Project:
    try:
        return load_package(path)
    except (PackageError, ValidationError) as exc:
        typer.secho(f"✗ invalid project: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _slug(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "realm"


def _stop_flag_path(realm_id: str) -> Path:
    d = Path.home() / ".agentrealm" / "realms"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{realm_id}.stop"


async def _chronicle() -> Chronicle:
    from agentrealm.chronicle import Chronicle
    from agentrealm.core.settings import load_settings

    return await Chronicle.connect(load_settings().database_url, create=True)


async def _status(realm_id: str) -> None:
    from agentrealm.chronicle import EventKind

    chron = await _chronicle()
    events = await chron.events(realm_id)
    if not events:
        typer.secho(f"no such realm {realm_id!r} (or nothing chronicled yet)", fg=typer.colors.RED)
        await chron.close()
        return
    lifecycle = [e for e in events if e.kind == EventKind.LIFECYCLE]
    state = lifecycle[-1].payload.get("event") if lifecycle else "unknown"
    typer.echo(f"realm {realm_id}: state={state}, {len(events)} events")
    spend = [e for e in events if e.kind == EventKind.SPEND]
    totals: dict[str, float] = {}
    for e in spend:
        totals[e.payload["agent"]] = totals.get(e.payload["agent"], 0.0) + float(e.payload["usd"])
    for agent, usd in sorted(totals.items()):
        typer.echo(f"  spend {agent}: ${usd:.4f}")
    await chron.close()


async def _tail(realm_id: str, limit: int) -> None:
    chron = await _chronicle()
    msgs = await chron.messages(realm_id)
    for m in msgs[-limit:]:
        typer.echo(f"[{m.ts_ms}] {m.channel} · {m.sender}: {m.body}")
    await chron.close()


async def _archive(realm_id: str, out: str) -> None:
    chron = await _chronicle()
    out_dir = Path(out) / realm_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript.txt").write_text(await chron.transcript(realm_id))
    report = await chron.final_report(realm_id, title=f"Realm {realm_id}")
    (out_dir / "report.md").write_text(report)
    typer.secho(f"archived to {out_dir}/", fg=typer.colors.GREEN)
    await chron.close()


async def _inject(realm_id: str, text: str) -> None:
    """Post an operator message into a live realm's commons (influence by message, never
    mid-run control). Finds the room from the realm's 'running' event; logs in as system."""
    import os

    from agentrealm.chronicle import EventKind
    from agentrealm.core.settings import load_settings
    from agentrealm.herald import Herald, HttpMatrixClient

    chron = await _chronicle()
    events = await chron.events(realm_id, kind=EventKind.LIFECYCLE)
    room = next(
        (e.payload["commons_room"] for e in reversed(events) if e.payload.get("commons_room")),
        None,
    )
    # mention the realm's agents so the message actually reaches them (Hermes needs mentions)
    msgs = await chron.messages(realm_id)
    agents = sorted({m.sender for m in msgs if m.sender.startswith(f"@{realm_id}-")})
    await chron.close()
    if not room:
        _die(f"no live commons room found for realm {realm_id!r} (is it running?)")

    s = load_settings()
    sys_pw = os.environ.get("AGENTREALM_SYSTEM_PASSWORD")
    if not sys_pw:
        _die("AGENTREALM_SYSTEM_PASSWORD not set (needed to post as the system account)")
    herald = Herald(
        HttpMatrixClient(s.matrix_homeserver),
        server_name=s.matrix_server_name, homeserver=s.matrix_homeserver_internal,
        operator=s.operator_user,
    )
    await herald.ensure_system(sys_pw)
    await herald.announce(str(room), f"[operator] {text}", mentions=agents or None)
    typer.secho(f"posted to realm {realm_id!r} commons", fg=typer.colors.GREEN)


async def _assist_repl(session: Any) -> None:  # pragma: no cover - interactive I/O
    """Read a user message, stream Scribe's turn to the terminal, repeat until exit/EOF."""
    from agentrealm.scribe.loop import LoopEvent

    def _show(event: LoopEvent) -> None:
        if event.kind == "tool_call":
            typer.secho(f"  🔧 {event.name} {event.text}", fg=typer.colors.MAGENTA)
        elif event.kind == "tool_result":
            body = event.text if len(event.text) <= 500 else event.text[:497] + "…"
            typer.secho(f"     {body}", fg=typer.colors.BRIGHT_BLACK)
        elif event.kind == "text":
            typer.echo(event.text)
        elif event.kind == "question":  # ask_user ended the turn — your next message answers it
            try:
                options = json.loads(event.name or "[]")
            except ValueError:
                options = []
            hint = f"  [{' / '.join(options)}]" if options else ""
            typer.secho(f"\nScribe asks: {event.text}{hint}\n", fg=typer.colors.GREEN)
        elif event.kind == "draft":  # propose_scenario — reply to refine, or tell it to create
            typer.secho(
                "\nScribe proposed a draft scenario (reply to refine it, or say "
                "'create it' to write it).\n",
                fg=typer.colors.GREEN,
            )
        elif event.kind == "notice":
            typer.secho(event.text, fg=typer.colors.BRIGHT_BLACK)
        elif event.kind == "final":
            typer.secho(f"\nScribe: {event.text}\n", fg=typer.colors.GREEN)

    while True:
        try:
            user = typer.prompt("you")
        except (EOFError, KeyboardInterrupt, typer.Abort):
            typer.echo()
            break
        if user.strip().lower() in ("exit", "quit"):
            break
        if not user.strip():
            continue
        try:
            async for event in session.send(user):
                _show(event)
        except Exception as exc:  # a backend error must not kill the session
            typer.secho(f"! {exc}", fg=typer.colors.RED, err=True)


async def _run_realm(realm_id: str, project: Project, require_mention: bool) -> str:
    """Wire the shared Platform and run the realm to conclusion."""
    from agentrealm.gatekeeper.service import ConfigError, build_platform

    try:
        platform = await build_platform()
    except ConfigError as exc:
        _die(str(exc))
    try:
        result = await platform.run(realm_id, project, require_mention=require_mention)
    finally:
        await platform.close()
    return result.report


def _die(msg: str) -> NoReturn:
    typer.secho(f"✗ {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


# Installed packages may contribute their own subcommands (entry-point group
# `agentrealm.commands`). This runs after every built-in command is registered, so a plugin sees a
# complete app; a plugin that fails costs you its command, never the CLI.
load_command_plugins(app)


if __name__ == "__main__":
    app()
