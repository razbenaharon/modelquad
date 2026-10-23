# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Automated repair command for degraded host integrations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import hostruntime
from .host_integrations import OFF_DETAIL, global_integrations_enabled
from .config import Config, load_config
from .doctor import CheckStatus, DoctorReport, run_doctor
from .onboarding import _find_host_descriptor, install_detected_hosts
from .registry import ProviderRegistry, default_registry

REPAIRABLE_EXACT_CHECKS = {
    "claude hook-gate runtime": "claude",
    "claude status line runtime": "claude",
    "claude automatic-routing hook (structural)": "claude",
}


#: Doctor check suffixes whose subject is a host integration, and whose remedy
#: is therefore "reinstall that host's adapter": reinstalling rewrites the
#: registration with the bare ``modelquad-mcp`` name, which is exactly what a
#: non-canonical registration needs. The same holds for the CLI-invoking hook
#: and status-line surfaces named explicitly above.
_REPAIRABLE_CHECK_SUFFIXES = (
    " adapter installed",
    " MCP registered",
    " MCP runtime",
)


def _map_check_to_host(check_name: str) -> str | None:
    """Map a doctor check name to its host integration ID, or None if unrepairable."""
    if check_name in REPAIRABLE_EXACT_CHECKS:
        return REPAIRABLE_EXACT_CHECKS[check_name]
    for suffix in _REPAIRABLE_CHECK_SUFFIXES:
        if check_name.endswith(suffix):
            host = check_name[: -len(suffix)].strip()
            return host
    return None


def run_repair(
    *,
    dry_run: bool = False,
    registry: ProviderRegistry | None = None,
    which: Callable[[str], str | None] | None = None,
    print_fn: Callable[..., None] = print,
    config: Config | None = None,
) -> dict[str, Any]:
    """Inspect doctor diagnostics and repair flagged, detected host integrations."""
    which = which if which is not None else hostruntime.discovery_which
    active_registry = registry or default_registry()
    config_obj = config if config is not None else load_config(None)

    # 1. Run doctor before report
    before_report = run_doctor(config_obj, which=which, registry=active_registry)
    before_warn_count = sum(1 for c in before_report.checks if c.status == CheckStatus.WARN)

    # 2. Identify repairable host integrations vs unrepairable items
    affected_hosts: list[str] = []
    skipped: list[str] = []
    integrations_enabled = global_integrations_enabled()
    if not integrations_enabled:
        print_fn(f"[skip] Host integrations: {OFF_DETAIL}")

    for check in before_report.checks:
        if check.status == CheckStatus.WARN:
            host_id = _map_check_to_host(check.name)
            if host_id is not None:
                if not integrations_enabled:
                    if check.name not in skipped:
                        skipped.append(check.name)
                    continue
                try:
                    descriptor = _find_host_descriptor(host_id, registry=active_registry)
                    detected = True if descriptor.detect is None else bool(descriptor.detect())
                except (OSError, ValueError):
                    detected = False
                if detected:
                    if host_id not in affected_hosts:
                        affected_hosts.append(host_id)
                else:
                    if check.name not in skipped:
                        skipped.append(check.name)
            else:
                if check.name not in skipped:
                    skipped.append(check.name)
        elif check.status == CheckStatus.MISSING:
            if check.name not in skipped:
                skipped.append(check.name)

    # 3. For each affected detected host, reinstall adapter, and report what
    # actually happened rather than what was attempted.
    repaired_hosts: list[str] = []
    failed_hosts: list[dict[str, Any]] = []
    for host_id in affected_hosts:
        verb = "Would reinstall" if dry_run else "Reinstalling"
        print_fn(f"[{'dry-run' if dry_run else 'repair'}] {verb} {host_id} adapter integration...")
        outcomes = install_detected_hosts([host_id], dry_run=dry_run, registry=active_registry)
        problems = [outcome for outcome in outcomes if outcome.detected and not outcome.ok]
        if problems or not outcomes:
            reason = "; ".join(
                f"{outcome.status.value}: {outcome.error}" if outcome.error else outcome.status.value
                for outcome in problems
            ) or "no result"
            failed_hosts.append({"host": host_id, "reason": reason})
            print_fn(f"[failed] {host_id}: {reason}")
            continue
        repaired_hosts.append(host_id)
        print_fn(f"[{'dry-run' if dry_run else 'ok'}] {host_id}: "
                 f"{'would be reinstalled' if dry_run else 'reinstalled'}")

    for item in skipped:
        reason = (OFF_DETAIL if not integrations_enabled and _map_check_to_host(item) is not None
                  else "not repairable automatically")
        print_fn(f"[skip] {item}: {reason}")

    # 4. Re-run doctor unless dry-run
    if dry_run:
        after_warn_count = before_warn_count
        ok = bool(before_report.ok) and not failed_hosts
        print_fn(f"Dry run complete: {len(repaired_hosts)} host integration(s) would be repaired.")
    else:
        after_report = run_doctor(config_obj, which=which, registry=active_registry)
        after_warn_count = sum(1 for c in after_report.checks if c.status == CheckStatus.WARN)
        ok = bool(after_report.ok) and not failed_hosts
        print_fn(
            f"Repair complete: {len(repaired_hosts)} host integration(s) repaired, "
            f"{len(failed_hosts)} failed. "
            f"Warnings before: {before_warn_count}, after: {after_warn_count}."
        )

    return {
        "repaired_hosts": repaired_hosts,
        "failed_hosts": failed_hosts,
        "global_host_integrations": "on" if integrations_enabled else "off",
        "skipped": skipped,
        "before_warn_count": before_warn_count,
        "after_warn_count": after_warn_count,
        "ok": ok,
    }
