from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Grid, Vertical
from textual.widgets import Footer, Header, RichLog, Static, DataTable

from config import Config
from logger import run_log
from output import output
from models.container import Container
from commands import CommandError, run_command, incus_exec, incus_command
from commands_snapshot import create_pre_update_snapshot, rollback_to_snapshot

DISCOVERY_TIMEOUT = 30
PANEL_COUNT = 4

# ============================================================================
# API / Core Functions (Engine)
# ============================================================================

async def get_all_containers() -> list[Container]:
    process = await asyncio.create_subprocess_exec(
        "incus", "list", "type=container", "--format", "json",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DISCOVERY_TIMEOUT)
    except asyncio.TimeoutError:
        try: process.kill()
        except ProcessLookupError: pass
        await process.wait()
        raise CommandError("Timeout while listing Incus containers")

    if process.returncode != 0:
        raise CommandError(stderr.decode(errors="replace").strip())

    try:
        instances = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse Incus JSON: {exc}") from exc

    result: list[Container] = []
    for instance in instances:
        if instance.get("type") != "container": continue
        name = instance.get("name")
        if not name: continue
        state = str(instance.get("status", "")).lower()
        result.append(Container(name=name, original_running=(state == "running")))
    result.sort(key=lambda c: c.name.lower())
    return result

def select_containers(containers: list[Container], config: Config) -> tuple[list[Container], set[str]]:
    available = {c.name for c in containers}
    if config.include:
        selected = [c for c in containers if c.name in config.include]
        missing = config.include - available
    else:
        selected = list(containers)
        missing = set()
    selected = [c for c in selected if c.name not in config.exclude]
    selected.sort(key=lambda c: c.name.lower())
    return selected, missing

async def read_os_release(container: Container) -> dict[str, str]:
    if container.original_running:
        process = await asyncio.create_subprocess_exec(
            "incus", "exec", container.name, "--", "cat", "/etc/os-release",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            "incus", "-q", "file", "pull", f"{container.name}/etc/os-release", "-",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DISCOVERY_TIMEOUT)
    except asyncio.TimeoutError:
        try: process.kill()
        except ProcessLookupError: pass
        await process.wait()
        raise CommandError(f"Timeout reading /etc/os-release from {container.name}")

    if process.returncode != 0:
        raise CommandError(stderr.decode(errors="replace").strip())

    result: dict[str, str] = {}
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    if not result:
        raise RuntimeError("Empty /etc/os-release")
    return result

def detect_package_manager(os_release: dict[str, str]) -> tuple[str, str]:
    distro_id = os_release.get("ID", "").lower()
    id_like = os_release.get("ID_LIKE", "").lower().split()
    identities = [distro_id, *id_like]

    if any(x in identities for x in ("debian", "ubuntu", "linuxmint", "kali")):
        return (os_release.get("PRETTY_NAME", distro_id), "apt")
    if any(x in identities for x in ("fedora", "rhel", "centos", "rocky", "almalinux", "ol")):
        return (os_release.get("PRETTY_NAME", distro_id), "dnf")
    if "arch" in identities:
        return (os_release.get("PRETTY_NAME", distro_id), "pacman")
    if "alpine" in identities:
        return (os_release.get("PRETTY_NAME", distro_id), "apk")
    if any(x in identities for x in ("suse", "opensuse")):
        return (os_release.get("PRETTY_NAME", distro_id), "zypper")
    return (os_release.get("PRETTY_NAME", distro_id or "unknown"), "unknown")

async def inspect_container(container: Container) -> None:
    os_release = await read_os_release(container)
    container.distro_name, container.package_manager = detect_package_manager(os_release)
    container.distro_id = os_release.get("ID", "unknown")
    container.status = "RUNNING" if container.original_running else "STOPPED"

async def wait_until_ready(container: Container) -> None:
    for _ in range(30):
        try:
            process = await asyncio.create_subprocess_exec(
                "incus", "exec", container.name, "--", "true",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            returncode = await asyncio.wait_for(process.wait(), timeout=5)
            if returncode == 0: return
        except (asyncio.TimeoutError, OSError): pass
        await asyncio.sleep(1)
    raise CommandError(f"Container {container.name} did not become ready")

async def start_container(container: Container) -> None:
    if container.original_running:
        container.log_event("container was already running")
        return
    container.log_event("starting container")
    await incus_command("start", container.name)
    container.started_by_us = True
    await wait_until_ready(container)
    container.log_event("container started and is ready")

async def stop_container(container: Container) -> None:
    if not container.started_by_us: return
    container.log_event("stopping container")
    try:
        await incus_command("stop", container.name)
        container.log_event("container stopped")
    except Exception as exc:
        container.log_event(f"WARNING: could not stop container: {exc}")

# ============================================================================
# Package Managers Specific (Optimiert für parallele Updates)
# ============================================================================
async def apt_refresh(c: Container) -> None:
    await incus_exec(c, "apt-get", "update")

async def apt_download(c: Container) -> None:
    await incus_exec(c, "apt-get", "-y", "--download-only", "upgrade")

async def apt_install(c: Container) -> None:
    await incus_exec(c, "apt-get", "-y", "--no-download", "upgrade")

async def pacman_refresh(c: Container) -> None:
    # -Sy aktualisiert nur die lokalen Paketdatenbanken
    await incus_exec(c, "pacman", "-Sy", "--noconfirm")

async def pacman_download(c: Container) -> None:
    await incus_exec(c, "pacman", "-Syu", "--downloadonly", "--noconfirm")

async def pacman_install(c: Container) -> None:
    await incus_exec(c, "pacman", "-Syu", "--noconfirm")

async def dnf_refresh(c: Container) -> None:
    await incus_exec(c, "dnf", "check-update") # Prüft Repos, wirft oft Code 100 bei Updates aus

async def dnf_download(c: Container) -> None:
    await incus_exec(c, "dnf", "upgrade", "-y", "--downloadonly")

async def dnf_install(c: Container) -> None:
    await incus_exec(c, "dnf", "upgrade", "-y", "--cacheonly")

async def zypper_refresh(c: Container) -> None:
    await incus_exec(c, "zypper", "--non-interactive", "refresh")

async def zypper_download(c: Container) -> None:
    await incus_exec(c, "zypper", "--non-interactive", "update", "--download-only")

async def zypper_install(c: Container) -> None:
    await incus_exec(c, "zypper", "--non-interactive", "update", "--no-remote")

async def apk_refresh(c: Container) -> None:
    await incus_exec(c, "apk", "update")

async def apk_install(c: Container) -> None:
    await incus_exec(c, "apk", "upgrade")


# --- NEU: Hilfsfunktionen zum Zählen der Updates ---

async def count_command_lines(*args: str) -> int:
    """Führt einen Befehl aus und zählt, wie viele Zeilen er ausgibt."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        lines = stdout.decode(errors="replace").strip().splitlines()
        return len(lines)
    except Exception:
        return 0

async def check_updates_count(container: Container) -> int:
    """Ermittelt die genaue Anzahl anstehender Updates im Container."""
    pm = container.package_manager
    
    if pm == "pacman":
        # pacman -Qu listet alle updatebaren Pakete auf. Exit Code 1 wenn keine da sind.
        process = await asyncio.create_subprocess_exec(
            "incus", "exec", container.name, "--", "pacman", "-Qu",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            lines = stdout.decode(errors="replace").strip().splitlines()
            return len(lines)
        except Exception:
            return 0

    elif pm == "apt":
        # apt list --upgradable gibt alle Pakete aus (+1 Header-Zeile)
        process = await asyncio.create_subprocess_exec(
            "incus", "exec", container.name, "--", "apt", "list", "--upgradable",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            lines = stdout.decode(errors="replace").strip().splitlines()
            count = len(lines) - 1 # Header abziehen
            return max(0, count)
        except Exception:
            return 0

    elif pm == "apk":
        # apk version -l '<' listet veraltete Pakete auf
        process = await asyncio.create_subprocess_exec(
            "incus", "exec", container.name, "--", "apk", "version", "-l", "<",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            lines = stdout.decode(errors="replace").strip().splitlines()
            return len(lines)
        except Exception:
            return 0

    elif pm == "dnf":
        # dnf check-update gibt Exit-Code 100 zurück, wenn Updates da sind
        process = await asyncio.create_subprocess_exec(
            "incus", "exec", container.name, "--", "dnf", "check-update", "-q",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
            if process.returncode == 100:
                return 1 # Signaliert "Updates vorhanden" (DNF-Zeilen-Parsing ist sehr fehleranfällig)
            return 0
        except Exception:
            return 0
            
    return 0



# --- Die neuen Weichensteller-Funktionen ---

async def package_refresh(container: Container) -> None:
    pm = container.package_manager
    if pm == "apt": await apt_refresh(container)
    elif pm == "pacman": await pacman_refresh(container)
    elif pm == "dnf": 
        try: await dnf_refresh(container)
        except Exception: pass # dnf check-update gibt Fehlercodes zurück, wenn Updates da sind
    elif pm == "zypper": await zypper_refresh(container)
    elif pm == "apk": await apk_refresh(container)
    else: raise RuntimeError(f"Unsupported package manager: {pm}")

async def package_download(container: Container) -> None:
    pm = container.package_manager
    if pm == "apt": await apt_download(container)
    elif pm == "pacman": await pacman_download(container)
    elif pm == "dnf": await dnf_download(container)
    elif pm == "zypper": await zypper_download(container)
    elif pm == "apk": pass # Alpine macht alles in einem Rutsch beim Installieren
    else: raise RuntimeError(f"Unsupported package manager: {pm}")

async def package_install(container: Container) -> None:
    pm = container.package_manager
    if pm == "apk": await apk_install(container)
    elif pm == "apt": await apt_install(container)
    elif pm == "pacman": await pacman_install(container)
    elif pm == "dnf": await dnf_install(container)
    elif pm == "zypper": await zypper_install(container)
    else: raise RuntimeError(f"Unsupported package manager: {pm}")

# ============================================================================
# Engine & UI Components
# ============================================================================

async def update_container(container: Container, config: Config, start_semaphore: asyncio.Semaphore, download_semaphore: asyncio.Semaphore) -> None:
    start_slot_acquired = False
    try:
        if not container.original_running:
            container.set_status("WAITING-START")
            container.log_event("waiting for start slot")
            await start_semaphore.acquire()
            start_slot_acquired = True
            container.log_event("start slot acquired")

        await start_container(container)

        if container.started_by_us and config.startup_delay_seconds > 0:
            container.set_status("STARTUP-WAIT")
            container.log_event(f"waiting {config.startup_delay_seconds}s for network/services")
            await asyncio.sleep(config.startup_delay_seconds)
            container.log_event("startup wait finished")

        # Phase 1: Paralleles Repo-Update
        container.set_status("REPO-UPDATE")
        container.log_event("REPO-UPDATE started")
        await package_refresh(container)
        container.log_event("REPO-UPDATE finished")

        # NEU: Updates zählen
        container.log_event("Checking for available updates...")
        count = await check_updates_count(container)
        container.update_count = count
        container.log_event(f"Found {count} update(s).")

        # NEU: Abkürzung, wenn der Container bereits up-to-date ist!
        if count == 0:
            container.log_event("No updates available. Container is up to date.")
            container.set_status("SUCCESS")
            return

		# ERWEITERUNG: Snapshot nur erstellen, wenn es in der config.toml AKTIVIERT ist
        if config.incus_snapshots:
            try:
                created_snapshot = await create_pre_update_snapshot(container)
            except Exception as snap_exc:
                raise RuntimeError(f"Snapshot creation failed: {snap_exc}")
        else:
            container.log_event("Snapshots are disabled in config.toml. Skipping creation.")
 
	    # Phase 2: Exklusiver Download (nur wenn Updates anstehen)
        container.set_status("WAITING-DOWNLOAD")
        container.log_event("waiting for download slot")

        async with download_semaphore:
            container.set_status("DOWNLOADING")
            container.log_event("DOWNLOAD started")
            await package_download(container)
            container.set_status("DOWNLOADED")
            container.log_event("DOWNLOAD finished")

        # Phase 3: Installation
        container.set_status("INSTALLING")
        container.log_event("INSTALL started")
        await package_install(container)
        container.set_status("SUCCESS")
        container.log_event("INSTALL finished successfully")
    except Exception as exc:
        container.error = str(exc)
        container.set_status("ERROR")
        container.log_event(f"ERROR: {exc}")
    finally:
        if container.started_by_us:
            await stop_container(container)
        if start_slot_acquired:
            start_semaphore.release()
            container.log_event("start slot released")


class ContainerPanel(Vertical):
    def __init__(self, panel_id: int) -> None:
        super().__init__(id=f"panel-{panel_id}")
        self.panel_id = panel_id
        self.container_name: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(" EMPTY", classes="panel-title")
        yield RichLog(wrap=True, markup=False, auto_scroll=True, classes="panel-log")

    def assign(self, container: Container) -> None:
        self.container_name = container.name
        self.query_one(".panel-title").update(f" {container.name} [{container.distro_name} / {container.package_manager}]")
        self.query_one(RichLog).clear()

    def clear_panel(self) -> None:
        self.container_name = None
        self.query_one(".panel-title").update(" EMPTY")
        self.query_one(RichLog).clear()

    def write(self, text: str) -> None:
        self.query_one(RichLog).write(text)

    def set_title(self, status: str) -> None:
        if self.container_name is None: return
        self.query_one(".panel-title").update(f" {self.container_name} [{status}]")


class IncusUpdaterApp(App):
    TITLE = "Incus Updater"
    CSS = """
    Screen { layout: vertical; }
    #container-overview { height: auto; max-height: 12; margin: 0 1; }
    #container-overview DataTable { height: auto; max-height: 12; }
    #container-grid { layout: grid; grid-size: 2 2; grid-columns: 1fr 1fr; grid-rows: 1fr 1fr; grid-gutter: 1; height: 1fr; padding: 0 1; }
    .panel { height: 100%; border: solid $primary; }
    .panel-title { height: 1; color: $text; background: $primary-darken-2; text-style: bold; padding: 0 1; }
    .panel-log { height: 1fr; background: $surface; color: $text; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, containers: list[Container], config: Config) -> None:
        super().__init__()
        self.containers = containers
        self.config = config
        self.panels: list[ContainerPanel] = []
        self.container_to_panel: dict[str, ContainerPanel] = {}
        self.next_container_index = 0
        self.finished_count = 0
        self.finished = False
        self.state_column_key = None
        self.updates_column_key = None  # Hier waren vermutlich Tabs reingerutscht


    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Container overview", id="overview-title")
        with Vertical(id="container-overview"):
            yield DataTable(id="container-table")
        yield Static("Starting...", id="status")
        with Grid(id="container-grid"):
            for index in range(PANEL_COUNT):
                panel = ContainerPanel(index)
                panel.add_class("panel")
                self.panels.append(panel)
                yield panel
        yield Footer()

    def on_mount(self) -> None:
        output.attach(self)
        self.populate_container_table()
        #self.assign_initial_panels()
        self.update_status("Updating...")
        self.run_worker(self.run_update(), exclusive=True)

    def populate_container_table(self) -> None:
        table = self.query_one("#container-table", DataTable)
        table.clear()

        # KORREKTUR: Wir fangen die Column-Keys ab (jetzt 5 Spalten statt 4)
        columns = table.add_columns("CONTAINER", "STATE", "UPDATES", "OS", "UPDATER")
        self.state_column_key = columns[1]    # Spalte STATE
        self.updates_column_key = columns[2]  # NEU: Spalte UPDATES

        for container in self.containers:
            state = "RUNNING" if container.original_running else "STOPPED"
            # Initial steht bei Updates ein "?"
            table.add_row(
                container.name, state, "?", container.distro_name, container.package_manager,
                key=container.name
            )

    def update_container_table_row(self, container: Container) -> None:
        """Aktualisiert die STATE- und UPDATES-Spalte in der oberen Übersicht live."""
        table = self.query_one("#container-table", DataTable)
        try:
            # 1. Update der STATE-Spalte
            if self.state_column_key is not None:
                status_text = container.status
                if container.status == "SUCCESS":
                    status_text = "[bold green]SUCCESS[/]"
                elif container.status == "ERROR":
                    status_text = "[bold red]ERROR[/]"
                elif container.status == "REPO-UPDATE":
                    status_text = "[bold cyan]REPO-UPDATE[/]"
                elif "DOWNLOADING" in container.status or "INSTALLING" in container.status:
                    status_text = f"[bold yellow]{container.status}[/]"
                table.update_cell(container.name, self.state_column_key, status_text)
            
            # 2. NEU: Update der UPDATES-Spalte (sobald REPO-UPDATE durch ist)
            if self.updates_column_key is not None and container.status != "DISCOVERED":
                if container.update_count == 0 and container.status in ("SUCCESS", "FINISHED", "REPO-UPDATE"):
                    # Falls nach dem Refresh 0 herauskam
                    table.update_cell(container.name, self.updates_column_key, "[green]0 Pakete[/]")
                elif container.update_count > 0:
                    table.update_cell(container.name, self.updates_column_key, f"[yellow]{container.update_count} Pakete[/]")
        except Exception:
            pass


#    def assign_initial_panels(self) -> None:
#        for panel in self.panels:
#            if self.next_container_index >= len(self.containers):
#                panel.clear_panel()
#                continue
#            container = self.containers[self.next_container_index]
#            self.next_container_index += 1
#            panel.assign(container)
#            self.container_to_panel[container.name] = panel
#
#    def recycle_panel(self, container: Container) -> None:
#        panel = self.container_to_panel.pop(container.name, None)
#        if panel is None: return
#        if self.next_container_index < len(self.containers):
#            next_container = self.containers[self.next_container_index]
#            self.next_container_index += 1
#            panel.assign(next_container)
#            self.container_to_panel[next_container.name] = panel
#            self.update_status(f"Running — {self.finished_count}/{len(self.containers)} finished")
#        else:
#            panel.clear_panel()

    def write(self, container_name: str, text: str) -> None:
        panel = self.container_to_panel.get(container_name)
        if panel is not None: panel.write(text)

    def status(self, container_name: str, status: str) -> None:
        panel = self.container_to_panel.get(container_name)
        if panel is not None: panel.set_title(status)

#    def container_finished(self, container: Container) -> None:
#        self.finished_count += 1
#        self.recycle_panel(container)

    def update_status(self, text: str) -> None:
        self.query_one("#status", Static).update(f" {text}")

    def global_message(self, text: str) -> None:
        self.update_status(text)

    async def run_update(self) -> None:
        """Führt die Updates über eine kontrollierte Warteschlange aus."""
        start_semaphore = asyncio.Semaphore(self.config.max_started_containers)
        download_semaphore = asyncio.Semaphore(self.config.max_downloads)

        # 1. Erstelle eine asynchrone Warteschlange und füttere sie mit allen Containern
        queue = asyncio.Queue()
        for container in self.containers:
            await queue.put(container)

        # 2. Ein Worker, der fest an ein Panel gebunden ist
        async def panel_worker(panel) -> None:
            while not queue.empty():
                try:
                    container = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                # Weise den Container dem Panel zu und starte das Update
                self.container_to_panel[container.name] = panel
                panel.assign(container)

                await update_container(container, self.config, start_semaphore, download_semaphore)

                # Update beendet -> Zähler hochsetzen und Verbindung lösen
                self.finished_count += 1
                self.container_to_panel.pop(container.name, None)
                self.update_status(f"Running — {self.finished_count}/{len(self.containers)} finished")
                
                queue.task_done()

            # Wenn dieser Worker keine Arbeit mehr hat, leert er sein Panel final
            panel.clear_panel()

        # 3. Starte exakt so viele Worker, wie wir Panels haben (PANEL_COUNT = 4)
        worker_tasks = []
        for panel in self.panels:
            task = asyncio.create_task(panel_worker(panel))
            worker_tasks.append(task)

        # Warte, bis alle 4 Panel-Worker ihre Arbeit beendet haben
        await asyncio.gather(*worker_tasks)

        # Zusammenfassung am Ende
        success = sum(1 for c in self.containers if c.status == "SUCCESS")
        failed = len(self.containers) - success
        self.finished = True
        self.update_status(f"Finished — {success} successful, {failed} failed")
        run_log.summary(f"SUMMARY: {success} successful, {failed} failed")
        output.global_message("Update finished.")


    def action_quit(self) -> None:
        output.detach()
        self.exit()
