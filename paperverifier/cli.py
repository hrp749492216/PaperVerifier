"""Command-line interface for PaperVerifier.

Provides three top-level commands:

* ``verify`` -- parse and verify a research paper, displaying results.
* ``apply``  -- apply selected feedback items to a paper.
* ``config`` -- interactively configure LLM providers and API keys.

All output is formatted with Rich for a professional terminal experience.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

from paperverifier import __version__
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table

import nest_asyncio

nest_asyncio.apply()  # For environments with existing event loops

console = Console()
error_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Severity display helpers
# ---------------------------------------------------------------------------

_SEVERITY_STYLES: dict[str, tuple[str, str]] = {
    # severity -> (badge_text, rich_style)
    "critical": ("[red bold]CRITICAL[/]", "red bold"),
    "major":    ("[dark_orange]MAJOR[/]",  "dark_orange"),
    "minor":    ("[yellow]MINOR[/]",       "yellow"),
    "info":     ("[blue]INFO[/]",          "blue"),
}

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def _severity_badge(severity: str) -> str:
    badge, _ = _SEVERITY_STYLES.get(severity, ("[dim]UNKNOWN[/]", "dim"))
    return badge


def _score_color(score: float | None) -> str:
    if score is None:
        return "dim"
    if score >= 8.0:
        return "green"
    if score >= 6.0:
        return "yellow"
    if score >= 4.0:
        return "dark_orange"
    return "red"


# ===================================================================
# CLI group
# ===================================================================

@click.group()
@click.version_option(version=__version__)
def cli():
    """PaperVerifier: Enterprise-grade research paper verification tool."""


# ===================================================================
# verify command
# ===================================================================

@cli.command()
@click.argument("input_path")
@click.option("--output", "-o", type=click.Path(), help="Output report path (JSON)")
@click.option(
    "--format", "-f", "output_format",
    type=click.Choice(["json", "markdown", "text"]),
    default="text",
    help="Output format (default: text)",
)
@click.option("--agents", "-a", help="Comma-separated agent names to run (default: all)")
@click.option(
    "--severity", "-s",
    type=click.Choice(["critical", "major", "minor", "info"]),
    help="Minimum severity to show",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def verify(input_path: str, output: str | None, output_format: str,
           agents: str | None, severity: str | None, verbose: bool):
    """Verify a research paper.

    INPUT_PATH can be a file path, URL, or GitHub repository URL.

    \b
    Examples:
      paperverifier verify paper.pdf
      paperverifier verify paper.pdf -o report.json -f json
      paperverifier verify https://arxiv.org/abs/2401.12345
      paperverifier verify https://github.com/user/paper-repo
    """
    asyncio.run(_verify(input_path, output, output_format, agents, severity, verbose))


async def _verify(
    input_path: str,
    output: str | None,
    output_format: str,
    agents: str | None,
    severity: str | None,
    verbose: bool,
) -> None:
    # ------------------------------------------------------------------
    # 1. Header
    # ------------------------------------------------------------------
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]PaperVerifier[/bold cyan]  [dim]v{__version__}[/dim]\n"
        "[dim]Enterprise-grade research paper verification[/dim]",
        border_style="cyan",
    ))
    console.print()

    # ------------------------------------------------------------------
    # 2. Load configuration and create LLM client
    # ------------------------------------------------------------------
    from paperverifier.config import get_settings, setup_logging
    from paperverifier.llm.client import UnifiedLLMClient
    from paperverifier.llm.config_store import load_role_assignments
    from paperverifier.llm.roles import AgentRole
    from paperverifier.parsers.router import InputRouter
    from paperverifier.agents.orchestrator import AgentOrchestrator

    settings = get_settings()
    setup_logging(level="DEBUG" if verbose else settings.log_level, fmt="console")

    assignments = load_role_assignments()
    client = UnifiedLLMClient()

    # Filter agents if --agents was provided
    if agents:
        requested = {name.strip().lower() for name in agents.split(",")}
        valid_roles = {r.value for r in AgentRole}
        unknown = requested - valid_roles
        if unknown:
            error_console.print(
                f"[yellow]Warning:[/] Unknown agent(s): {', '.join(sorted(unknown))}. "
                f"Valid agents: {', '.join(sorted(valid_roles))}"
            )
        # Keep only requested roles in assignments
        assignments = {
            role: assn for role, assn in assignments.items()
            if role.value in requested
        }

    # ------------------------------------------------------------------
    # 3. Parse document
    # ------------------------------------------------------------------
    router = InputRouter()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description="Parsing document...", total=None)
        try:
            document = await router.parse(input_path)
        except FileNotFoundError:
            error_console.print(f"[red]Error:[/] File not found: {input_path}")
            sys.exit(1)
        except Exception as exc:
            error_console.print(f"[red]Error parsing document:[/] {exc}")
            if verbose:
                console.print_exception()
            sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Document summary
    # ------------------------------------------------------------------
    doc_title = document.title or "Untitled Document"
    section_count = len(document.sections)
    ref_count = len(document.references)
    word_count = len(document.full_text.split())

    doc_info = Table.grid(padding=(0, 2))
    doc_info.add_column(style="bold", justify="right")
    doc_info.add_column()
    doc_info.add_row("Title:", escape(doc_title))
    doc_info.add_row("Sections:", str(section_count))
    doc_info.add_row("References:", str(ref_count))
    doc_info.add_row("Words:", f"{word_count:,}")
    if document.source_type:
        doc_info.add_row("Source:", document.source_type)

    console.print(Panel(doc_info, title="[bold]Document Summary[/bold]", border_style="blue"))
    console.print()

    # ------------------------------------------------------------------
    # 5. Run verification agents
    # ------------------------------------------------------------------
    agent_statuses: dict[str, str] = {}

    async def progress_callback(role_name: str, status: str) -> None:
        agent_statuses[role_name] = status

    orchestrator = AgentOrchestrator(
        client=client,
        assignments=assignments,
        max_concurrent=settings.max_concurrent_agents,
        progress_callback=progress_callback,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            description="Running verification agents...", total=None,
        )
        try:
            report = await orchestrator.verify(document)
        except Exception as exc:
            error_console.print(f"[red]Verification failed:[/] {exc}")
            if verbose:
                console.print_exception()
            sys.exit(1)

    console.print()

    # ------------------------------------------------------------------
    # 6. Display results
    # ------------------------------------------------------------------
    _display_report_summary(report)
    console.print()

    # Findings table
    all_findings = report._all_findings()
    if all_findings:
        _display_findings(all_findings, min_severity=severity)
    else:
        console.print("[green]No findings detected. The paper looks good![/green]")

    console.print()

    # Agent status table
    _display_agent_status(report)
    console.print()

    # Token usage (verbose only)
    if verbose and report.total_tokens:
        token_table = Table(title="Token Usage", border_style="dim")
        token_table.add_column("Metric", style="bold")
        token_table.add_column("Count", justify="right")
        token_table.add_row(
            "Input tokens",
            f"{report.total_tokens.get('input_tokens', 0):,}",
        )
        token_table.add_row(
            "Output tokens",
            f"{report.total_tokens.get('output_tokens', 0):,}",
        )
        if report.estimated_cost_usd is not None:
            token_table.add_row(
                "Estimated cost",
                f"${report.estimated_cost_usd:.4f}",
            )
        console.print(token_table)
        console.print()

    # ------------------------------------------------------------------
    # 7. Save report if --output specified
    # ------------------------------------------------------------------
    if output:
        output_path = Path(output)
        try:
            if output_format == "json":
                output_path.write_text(report.to_json(), encoding="utf-8")
            elif output_format == "markdown":
                output_path.write_text(
                    _report_to_markdown(report), encoding="utf-8",
                )
            else:
                output_path.write_text(
                    _report_to_text(report), encoding="utf-8",
                )
            console.print(
                f"[green]Report saved to:[/green] {output_path.resolve()}"
            )
        except OSError as exc:
            error_console.print(f"[red]Failed to save report:[/] {exc}")
            sys.exit(1)


# ===================================================================
# apply command
# ===================================================================

@cli.command()
@click.argument("input_path")
@click.argument("report_path", type=click.Path(exists=True))
@click.option("--items", "-i", required=True,
              help="Comma-separated item numbers to apply (e.g., 1,3,5-8)")
@click.option("--output", "-o", type=click.Path(), help="Output file path")
@click.option("--diff", "show_diff", is_flag=True,
              help="Show diff instead of writing output")
def apply(input_path: str, report_path: str, items: str,
          output: str | None, show_diff: bool):
    """Apply selected feedback items to a paper.

    \b
    Examples:
      paperverifier apply paper.pdf report.json --items 1,3,5 -o paper_fixed.pdf
      paperverifier apply paper.pdf report.json --items 1-8 --diff
    """
    asyncio.run(_apply(input_path, report_path, items, output, show_diff))


async def _apply(
    input_path: str,
    report_path: str,
    items_str: str,
    output: str | None,
    show_diff: bool,
) -> None:
    from paperverifier.feedback.applier import FeedbackApplier, FeedbackConflictError
    from paperverifier.feedback.diff_generator import DiffGenerator
    from paperverifier.models.report import VerificationReport
    from paperverifier.parsers.router import InputRouter
    from paperverifier.llm.client import UnifiedLLMClient
    from paperverifier.llm.config_store import load_role_assignments
    from paperverifier.llm.roles import AgentRole

    console.print()
    console.print(Panel.fit(
        "[bold cyan]PaperVerifier[/bold cyan]  [dim]Feedback Applier[/dim]",
        border_style="cyan",
    ))
    console.print()

    # ------------------------------------------------------------------
    # 1. Parse item numbers
    # ------------------------------------------------------------------
    try:
        selected_items = _parse_item_numbers(items_str)
    except ValueError as exc:
        error_console.print(f"[red]Invalid item numbers:[/] {exc}")
        sys.exit(1)

    console.print(f"[bold]Selected items:[/] {', '.join(str(n) for n in selected_items)}")
    console.print()

    # ------------------------------------------------------------------
    # 2. Load the report
    # ------------------------------------------------------------------
    try:
        report_data = Path(report_path).read_text(encoding="utf-8")
        report = VerificationReport.model_validate_json(report_data)
    except (OSError, json.JSONDecodeError, Exception) as exc:
        error_console.print(f"[red]Failed to load report:[/] {exc}")
        sys.exit(1)

    console.print(
        f"[dim]Loaded report with {len(report.feedback_items)} feedback items[/dim]"
    )

    # ------------------------------------------------------------------
    # 3. Parse the document
    # ------------------------------------------------------------------
    router = InputRouter()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description="Parsing document...", total=None)
        try:
            document = await router.parse(input_path)
        except Exception as exc:
            error_console.print(f"[red]Error parsing document:[/] {exc}")
            sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Apply feedback
    # ------------------------------------------------------------------
    assignments = load_role_assignments()
    writer_assignment = assignments.get(AgentRole.WRITER)
    client = UnifiedLLMClient()

    applier = FeedbackApplier(
        client=client,
        writer_assignment=writer_assignment,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description="Applying feedback...", total=None)
        try:
            result = await applier.apply(
                document, report, selected_items, force=True,
            )
        except FeedbackConflictError as exc:
            error_console.print(f"[red]Conflict detected:[/] {exc}")
            sys.exit(1)
        except Exception as exc:
            error_console.print(f"[red]Failed to apply feedback:[/] {exc}")
            sys.exit(1)

    console.print()

    # Summary table
    summary_table = Table.grid(padding=(0, 2))
    summary_table.add_column(style="bold", justify="right")
    summary_table.add_column()
    summary_table.add_row("Applied:", f"[green]{len(result.applied_items)}[/green]")
    summary_table.add_row("Skipped:", f"[yellow]{len(result.skipped_items)}[/yellow]")
    if result.errors:
        summary_table.add_row("Errors:", f"[red]{len(result.errors)}[/red]")
    console.print(Panel(summary_table, title="[bold]Application Summary[/bold]", border_style="green"))
    console.print()

    # Show errors/warnings
    if result.errors:
        for err in result.errors:
            error_console.print(f"  [yellow]![/] {err}")
        console.print()

    # ------------------------------------------------------------------
    # 5. Show diff or write output
    # ------------------------------------------------------------------
    if show_diff:
        diff_text = DiffGenerator.unified_diff(
            result.original_text,
            result.modified_text,
            filename=Path(input_path).name,
        )
        if diff_text:
            console.print(Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=False),
                title="[bold]Diff[/bold]",
                border_style="cyan",
            ))
        else:
            console.print("[dim]No changes were made.[/dim]")
    elif output:
        output_path = Path(output)
        try:
            output_path.write_text(result.modified_text, encoding="utf-8")
            console.print(f"[green]Modified document saved to:[/] {output_path.resolve()}")
        except OSError as exc:
            error_console.print(f"[red]Failed to write output:[/] {exc}")
            sys.exit(1)
    else:
        # No output specified and no diff flag: show summary from DiffGenerator
        summary_text = DiffGenerator.summary(result)
        console.print(summary_text)


# ===================================================================
# config command
# ===================================================================

@cli.command()
def config():
    """Configure LLM providers and API keys.

    Interactive setup for API keys and role assignments.
    API keys are stored securely in your OS keyring.
    """
    from paperverifier.llm.providers import LLMProvider, PROVIDER_REGISTRY
    from paperverifier.llm.config_store import (
        get_api_key,
        store_api_key,
        load_role_assignments,
        save_role_assignments,
        list_configured_providers,
    )
    from paperverifier.llm.roles import AgentRole, DEFAULT_ASSIGNMENTS
    from paperverifier.llm.client import UnifiedLLMClient

    console.print()
    console.print(Panel.fit(
        "[bold cyan]PaperVerifier[/bold cyan]  [dim]Configuration[/dim]",
        border_style="cyan",
    ))
    console.print()

    # ------------------------------------------------------------------
    # Show current provider status
    # ------------------------------------------------------------------
    configured_providers = list_configured_providers()

    provider_table = Table(
        title="LLM Provider Status",
        border_style="blue",
        header_style="bold",
    )
    provider_table.add_column("#", style="dim", width=4)
    provider_table.add_column("Provider", style="bold")
    provider_table.add_column("Status")
    provider_table.add_column("Env Var", style="dim")
    provider_table.add_column("Models", style="dim")

    for idx, provider in enumerate(LLMProvider, start=1):
        spec = PROVIDER_REGISTRY[provider]
        is_configured = provider in configured_providers
        status = "[green]Configured[/]" if is_configured else "[dim]Not set[/dim]"
        models = ", ".join(spec.default_models[:2])
        if len(spec.default_models) > 2:
            models += ", ..."
        provider_table.add_row(
            str(idx),
            spec.display_name,
            status,
            f"${spec.env_var}",
            models,
        )

    console.print(provider_table)
    console.print()

    # ------------------------------------------------------------------
    # Prompt for API keys
    # ------------------------------------------------------------------
    if click.confirm("Would you like to configure API keys?", default=False):
        console.print()
        provider_list = list(LLMProvider)
        for idx, provider in enumerate(provider_list, start=1):
            spec = PROVIDER_REGISTRY[provider]
            existing = get_api_key(provider)
            existing_label = " [green](configured)[/]" if existing else ""
            console.print(f"  {idx}. {spec.display_name}{existing_label}")

        console.print()
        selection = click.prompt(
            "Enter provider numbers to configure (comma-separated, or 'skip')",
            default="skip",
        )

        if selection.strip().lower() != "skip":
            try:
                selected_indices = _parse_item_numbers(selection)
            except ValueError:
                error_console.print("[yellow]Invalid selection, skipping.[/]")
                selected_indices = []

            for idx in selected_indices:
                if idx < 1 or idx > len(provider_list):
                    error_console.print(f"[yellow]Invalid provider number: {idx}[/]")
                    continue

                provider = provider_list[idx - 1]
                spec = PROVIDER_REGISTRY[provider]
                console.print()
                console.print(f"[bold]{spec.display_name}[/bold] ({spec.env_var})")

                api_key = click.prompt(
                    f"  Enter API key for {spec.display_name}",
                    hide_input=True,
                    default="",
                    show_default=False,
                )
                if api_key.strip():
                    try:
                        store_api_key(provider, api_key.strip())
                        console.print(f"  [green]API key saved to keyring.[/green]")
                    except Exception as exc:
                        error_console.print(f"  [red]Failed to store key:[/] {exc}")
                else:
                    console.print("  [dim]Skipped.[/dim]")

    console.print()

    # ------------------------------------------------------------------
    # Show current role assignments
    # ------------------------------------------------------------------
    assignments = load_role_assignments()

    role_table = Table(
        title="Agent Role Assignments",
        border_style="blue",
        header_style="bold",
    )
    role_table.add_column("Role", style="bold")
    role_table.add_column("Provider")
    role_table.add_column("Model")
    role_table.add_column("Temperature", justify="right")
    role_table.add_column("Max Tokens", justify="right")

    for role in AgentRole:
        assn = assignments.get(role)
        if assn is None:
            role_table.add_row(role.value, "[dim]Not assigned[/dim]", "", "", "")
        else:
            role_table.add_row(
                role.value,
                assn.provider.value,
                assn.model,
                f"{assn.temperature:.1f}",
                f"{assn.max_tokens:,}",
            )

    console.print(role_table)
    console.print()

    # ------------------------------------------------------------------
    # Test connections
    # ------------------------------------------------------------------
    if click.confirm("Would you like to test provider connections?", default=False):
        console.print()
        client = UnifiedLLMClient()
        providers_to_test = list_configured_providers()

        if not providers_to_test:
            console.print("[yellow]No providers configured. Set API keys first.[/yellow]")
        else:
            test_table = Table(
                title="Connection Tests",
                border_style="blue",
                header_style="bold",
            )
            test_table.add_column("Provider", style="bold")
            test_table.add_column("Result")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
                transient=True,
            ) as progress:
                for provider in providers_to_test:
                    spec = PROVIDER_REGISTRY[provider]
                    progress.add_task(
                        description=f"Testing {spec.display_name}...",
                        total=None,
                    )
                    ok = asyncio.run(client.test_connection(provider))
                    result = "[green]Connected[/]" if ok else "[red]Failed[/]"
                    test_table.add_row(spec.display_name, result)

            console.print(test_table)

    console.print()
    console.print("[dim]Configuration complete.[/dim]")
    console.print()


# ===================================================================
# Display helpers
# ===================================================================

def _parse_item_numbers(items_str: str) -> list[int]:
    """Parse item number string like '1,3,5-8,10' into [1,3,5,6,7,8,10].

    Supports individual numbers and inclusive ranges separated by commas.

    Raises:
        ValueError: If the string contains invalid tokens.
    """
    result: list[int] = []
    for part in items_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = int(bounds[0].strip())
                end = int(bounds[1].strip())
            except ValueError:
                raise ValueError(f"Invalid range: '{part}'") from None
            if start > end:
                raise ValueError(
                    f"Invalid range: '{part}' (start > end)"
                )
            result.extend(range(start, end + 1))
        else:
            try:
                result.append(int(part))
            except ValueError:
                raise ValueError(f"Invalid number: '{part}'") from None
    return sorted(set(result))


def _display_findings(
    findings: list,
    min_severity: str | None = None,
) -> None:
    """Display findings with Rich formatting.

    Filters by minimum severity when specified and renders a numbered
    table with colored severity badges.
    """
    from paperverifier.models.findings import Finding

    # Filter by severity if requested
    if min_severity:
        threshold = _SEVERITY_ORDER.get(min_severity, 3)
        findings = [
            f for f in findings
            if _SEVERITY_ORDER.get(f.severity.value, 3) <= threshold
        ]

    if not findings:
        console.print("[dim]No findings at the selected severity level.[/dim]")
        return

    table = Table(
        title=f"Findings ({len(findings)})",
        border_style="blue",
        header_style="bold",
        show_lines=True,
        expand=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Severity", width=10, justify="center")
    table.add_column("Category", width=14)
    table.add_column("Title", ratio=2)
    table.add_column("Location", width=20, style="dim")

    for idx, finding in enumerate(findings, start=1):
        sev = finding.severity.value
        badge = _severity_badge(sev)
        segment = finding.segment_id or ""
        table.add_row(
            str(idx),
            badge,
            finding.category.value,
            escape(finding.title),
            segment,
        )

    console.print(table)

    # Detailed descriptions below the table
    console.print()
    for idx, finding in enumerate(findings, start=1):
        sev = finding.severity.value
        badge = _severity_badge(sev)
        header = f"[bold]#{idx}[/bold] {badge}  [bold]{escape(finding.title)}[/bold]"
        detail_lines: list[str] = [
            f"[dim]Category:[/dim] {finding.category.value}",
            f"[dim]Agent:[/dim]    {finding.agent_role}",
            "",
            escape(finding.description),
        ]
        if finding.segment_text:
            preview = finding.segment_text[:200]
            if len(finding.segment_text) > 200:
                preview += "..."
            detail_lines.append("")
            detail_lines.append(f'[dim]Text:[/dim] "{escape(preview)}"')
        if finding.suggestion:
            detail_lines.append("")
            detail_lines.append(f"[green]Suggestion:[/green] {escape(finding.suggestion)}")
        if finding.evidence:
            detail_lines.append("")
            detail_lines.append("[dim]Evidence:[/dim]")
            for ev in finding.evidence:
                detail_lines.append(f"  - {escape(ev)}")

        _, style = _SEVERITY_STYLES.get(sev, ("[dim]UNKNOWN[/]", "dim"))
        console.print(Panel(
            "\n".join(detail_lines),
            title=header,
            border_style=style,
            padding=(0, 1),
        ))


def _display_report_summary(report) -> None:
    """Display a summary panel with overall score and stats."""
    from paperverifier.models.report import VerificationReport

    # Build score display
    score = report.overall_score
    if score is not None:
        score_str = f"[{_score_color(score)} bold]{score:.1f}[/] / 10"
    else:
        score_str = "[dim]N/A[/dim]"

    # Severity breakdown
    severity_parts: list[str] = []
    for sev_name in ("critical", "major", "minor", "info"):
        count = report.severity_counts.get(sev_name, 0)
        if count > 0:
            badge = _severity_badge(sev_name)
            severity_parts.append(f"{badge} {count}")

    severity_str = "  ".join(severity_parts) if severity_parts else "[dim]None[/dim]"

    # Build the summary grid
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", justify="right", min_width=16)
    grid.add_column()
    grid.add_row("Overall Score:", score_str)
    grid.add_row("Total Findings:", str(report.total_findings))
    grid.add_row("Severity:", severity_str)
    grid.add_row(
        "Agents:",
        f"{report.agents_completed}/{report.agents_total} completed",
    )
    grid.add_row("Duration:", f"{report.duration_seconds:.1f}s")

    console.print(Panel(
        grid,
        title="[bold]Verification Results[/bold]",
        border_style="green" if report.total_findings == 0 else "yellow",
        padding=(1, 2),
    ))


def _display_agent_status(report) -> None:
    """Display a table showing which agents completed, failed, or were disabled."""
    table = Table(
        title="Agent Status",
        border_style="blue",
        header_style="bold",
    )
    table.add_column("Agent", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Provider", style="dim")
    table.add_column("Model", style="dim")
    table.add_column("Findings", justify="right")
    table.add_column("Error", style="dim", max_width=40)

    for agent_report in report.agent_reports:
        status = agent_report.status
        if status == "completed":
            status_str = "[green]Completed[/]"
        elif status == "failed":
            status_str = "[red]Failed[/]"
        elif status == "disabled":
            status_str = "[yellow]Disabled[/]"
        else:
            status_str = f"[dim]{status}[/dim]"

        error_msg = ""
        if agent_report.error_message:
            error_msg = agent_report.error_message[:40]
            if len(agent_report.error_message) > 40:
                error_msg += "..."

        table.add_row(
            agent_report.agent_role,
            status_str,
            agent_report.provider or "",
            agent_report.model or "",
            str(len(agent_report.findings)),
            escape(error_msg),
        )

    console.print(table)


# ===================================================================
# Report serialisation helpers
# ===================================================================

def _report_to_markdown(report) -> str:
    """Convert a VerificationReport to Markdown text."""
    lines: list[str] = []
    lines.append(f"# PaperVerifier Report")
    lines.append("")
    if report.document_title:
        lines.append(f"**Document:** {report.document_title}")
    lines.append(f"**Date:** {report.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(
        f"**Agents:** {report.agents_completed}/{report.agents_total} completed"
    )
    lines.append(f"**Duration:** {report.duration_seconds:.1f}s")
    lines.append("")

    if report.overall_score is not None:
        lines.append(f"## Overall Score: {report.overall_score:.1f}/10")
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(report.summary)
    lines.append("")

    # Severity counts
    lines.append("## Findings by Severity")
    lines.append("")
    for sev in ("critical", "major", "minor", "info"):
        count = report.severity_counts.get(sev, 0)
        lines.append(f"- **{sev.capitalize()}:** {count}")
    lines.append("")

    # All findings
    all_findings = report._all_findings()
    if all_findings:
        lines.append(f"## Detailed Findings ({len(all_findings)})")
        lines.append("")
        for idx, finding in enumerate(all_findings, start=1):
            lines.append(f"### #{idx}: {finding.title}")
            lines.append("")
            lines.append(f"- **Severity:** {finding.severity.value}")
            lines.append(f"- **Category:** {finding.category.value}")
            lines.append(f"- **Agent:** {finding.agent_role}")
            if finding.segment_id:
                lines.append(f"- **Location:** {finding.segment_id}")
            lines.append("")
            lines.append(finding.description)
            if finding.suggestion:
                lines.append("")
                lines.append(f"> **Suggestion:** {finding.suggestion}")
            if finding.evidence:
                lines.append("")
                lines.append("**Evidence:**")
                for ev in finding.evidence:
                    lines.append(f"- {ev}")
            lines.append("")

    # Agent status
    lines.append("## Agent Status")
    lines.append("")
    lines.append("| Agent | Status | Provider | Model | Findings |")
    lines.append("|-------|--------|----------|-------|----------|")
    for ar in report.agent_reports:
        lines.append(
            f"| {ar.agent_role} | {ar.status} | {ar.provider} "
            f"| {ar.model} | {len(ar.findings)} |"
        )
    lines.append("")

    return "\n".join(lines)


def _report_to_text(report) -> str:
    """Convert a VerificationReport to plain text."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("PAPERVERIFIER REPORT")
    lines.append("=" * 70)
    lines.append("")
    if report.document_title:
        lines.append(f"Document:  {report.document_title}")
    lines.append(
        f"Date:      {report.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    lines.append(
        f"Agents:    {report.agents_completed}/{report.agents_total} completed"
    )
    lines.append(f"Duration:  {report.duration_seconds:.1f}s")
    if report.overall_score is not None:
        lines.append(f"Score:     {report.overall_score:.1f}/10")
    lines.append("")

    lines.append("SUMMARY")
    lines.append("-" * 70)
    lines.append(report.summary)
    lines.append("")

    # Severity
    lines.append("SEVERITY BREAKDOWN")
    lines.append("-" * 70)
    for sev in ("critical", "major", "minor", "info"):
        count = report.severity_counts.get(sev, 0)
        lines.append(f"  {sev.upper():<12} {count}")
    lines.append("")

    # Findings
    all_findings = report._all_findings()
    if all_findings:
        lines.append(f"FINDINGS ({len(all_findings)})")
        lines.append("-" * 70)
        for idx, finding in enumerate(all_findings, start=1):
            lines.append(
                f"\n  #{idx}  [{finding.severity.value.upper()}] {finding.title}"
            )
            lines.append(f"       Category: {finding.category.value}")
            lines.append(f"       Agent:    {finding.agent_role}")
            if finding.segment_id:
                lines.append(f"       Location: {finding.segment_id}")
            lines.append(f"       {finding.description}")
            if finding.suggestion:
                lines.append(f"       Suggestion: {finding.suggestion}")
        lines.append("")

    # Agent status
    lines.append("AGENT STATUS")
    lines.append("-" * 70)
    for ar in report.agent_reports:
        status_sym = "OK" if ar.status == "completed" else ar.status.upper()
        lines.append(
            f"  [{status_sym:<10}] {ar.agent_role:<28} "
            f"{ar.provider}/{ar.model}  ({len(ar.findings)} findings)"
        )
        if ar.error_message:
            lines.append(f"               Error: {ar.error_message}")
    lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)


# ===================================================================
# Entry point
# ===================================================================

def main() -> None:
    """Entry point for ``python -m paperverifier`` or console_scripts."""
    cli()


if __name__ == "__main__":
    main()
