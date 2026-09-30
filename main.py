#!/usr/bin/env python3
from __future__ import annotations
import argparse
import asyncio
import sys

from .config import load_config, Config
from .logger import run_log
from .output import output
from .models.container import Container
from .ui.app import (
    get_all_containers, select_containers, inspect_container, 
    update_container, IncusUpdaterApp
)

async def do_dry_run(config: Config) -> int:
    all_containers = await get_all_containers()
    containers, missing = select_containers(all_containers, config)

    print("\nIncus container discovery\n" + "=" * 130 + "\n")
    if config.include:
        print("POSITIVE LIST:")
        for name in sorted(config.include): print(f"  + {name}")
        print()
    if config.exclude:
        print("EXCLUSION LIST:")
        for name in sorted(config.exclude): print(f"  - {name}")
        print()
    if missing:
        print("WARNING: not found:")
        for name in sorted(missing): print(f"  ! {name}")
        print()

    print(f"{'CONTAINER':<30}{'STATE':<12}{'UPDATES':<15}{'DISTRIBUTION':<35}{'PACKAGE MANAGER'}\n" + "-" * 130)

    for container in containers:
        state = "RUNNING" if container.original_running else "STOPPED"
        try:
            await inspect_container(container)
            # Hier geben wir einfach "0" oder "?" aus, da im Dry-Run noch nicht gezählt wird
            print(f"{container.name:<30}{state:<12}{'?':<15}{container.distro_name:<35}{container.package_manager}")
        except Exception as exc:
            print(f"{container.name:<30}{state:<12}{'ERROR':<15}{'ERROR: ' + str(exc):<35}")

    print("\n" + "-" * 130 + f"\n{len(containers)} container(s) selected.\n{len(all_containers)} container(s) found.\n\nNo container was started or stopped.\nDRY RUN: no package updates were performed.\n")
    return 0

async def do_update(config: Config, containers: list[Container]) -> int:
    print(f"\nStarting Incus update\n" + "=" * 90)
    print(f"Selected containers: {len(containers)}\nMax. containers started by us: {config.max_started_containers}\nMax. simultaneous downloads: {config.max_downloads}\nStartup delay: {config.startup_delay_seconds}s\n")

    start_semaphore = asyncio.Semaphore(config.max_started_containers)
    download_semaphore = asyncio.Semaphore(config.max_downloads)

    tasks = [asyncio.create_task(update_container(container, config, start_semaphore, download_semaphore)) for container in containers]
    await asyncio.gather(*tasks)

    print("\n" + "=" * 90 + "\nUPDATE SUMMARY\n" + "=" * 90)
    success, failed = 0, 0
    for container in containers:
        if container.status == "SUCCESS":
            print(f"OK       {container.name}")
            success += 1
        else:
            print(f"FAILED   {container.name}")
            if container.error: print(f"         {container.error}")
            failed += 1

    print(f"\nSuccessful: {success}\nFailed:     {failed}")
    run_log.summary(f"SUMMARY: {success} successful, {failed} failed")
    return 0 if failed == 0 else 1

async def prepare_containers_local(config: Config):
    all_containers = await get_all_containers()
    containers, missing = select_containers(all_containers, config)
    valid, invalid = [], []
    for container in containers:
        try:
            await inspect_container(container)
            if container.package_manager == "unknown":
                raise RuntimeError(f"Unsupported distribution: {container.distro_name}")
            valid.append(container)
        except Exception as exc:
            container.error, container.status = str(exc), "ERROR"
            invalid.append(container)
    return valid, invalid, missing

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parallel Incus container updater")
    parser.add_argument("--dry-run", action="store_true", help="Discover containers without starting or updating anything.")
    parser.add_argument("--ui", action="store_true", help="Run the updater with a 2x2 terminal UI.")
    return parser.parse_args()

def print_ui_summary(containers: list[Container]) -> None:
    print("\n" + "=" * 90 + "\nUPDATE SUMMARY\n" + "=" * 90)
    success, failed = 0, 0
    for container in containers:
        if container.status == "SUCCESS":
            print(f"OK       {container.name}")
            success += 1
        else:
            print(f"FAILED   {container.name}")
            if container.error: print(f"         {container.error}")
            failed += 1
    print(f"\nSuccessful: {success}\nFailed:     {failed}")

async def async_main(args: argparse.Namespace) -> int:
    config = load_config()
    if args.dry_run: return await do_dry_run(config)

    containers, invalid, missing = await prepare_containers_local(config)
    if missing:
        print("WARNING: containers from include list were not found:")
        for name in sorted(missing): print(f"  {name}")
        print()
    if invalid:
        print("WARNING: containers skipped because discovery failed:")
        for container in invalid: print(f"  {container.name}: {container.error}")
        print()
    if not containers:
        print("No usable containers selected.")
        return 1

    run_log.event(None, f"Selected {len(containers)} container(s)")
    run_log.event(None, f"max_started_containers={config.max_started_containers}, max_downloads={config.max_downloads}, startup_delay_seconds={config.startup_delay_seconds}")

    if args.ui:
        app = IncusUpdaterApp(containers, config)
        await app.run_async()
        print_ui_summary(containers)
        return 0 if all(c.status == "SUCCESS" for c in containers) else 1

    return await do_update(config, containers)

def main() -> None:
    try:
        args = parse_arguments()
        exit_code = asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        run_log.summary("INTERRUPTED")
        exit_code = 130
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        run_log.summary(f"FATAL ERROR: {exc}")
        exit_code = 1
    raise SystemExit(exit_code)

if __name__ == "__main__":
    main()
