import asyncio
import time
from incus_updater.models.container import Container
from incus_updater.commands import incus_command, CommandError

async def create_pre_update_snapshot(container: Container) -> str:
    """
    Erstellt einen automatischen Backup-Snapshot vor dem Update.
    Gibt den Namen des erstellten Snapshots zurück.
    """
    # Generiert einen eindeutigen Namen mit Zeitstempel
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot_name = f"pre-update-{timestamp}"
    
    container.log_event(f"Creating snapshot: {snapshot_name}...")
    
    # Entspricht im Terminal: incus snapshot create <container_name> <snapshot_name>
    await incus_command("snapshot", "create", container.name, snapshot_name)
    
    container.log_event("Snapshot created successfully.")
    return snapshot_name

async def rollback_to_snapshot(container: Container, snapshot_name: str) -> None:
    """
    Rollt den Container im Fehlerfall auf den Zustand des Snapshots zurück.
    """
    container.log_event(f"CRITICAL: Rolling back to snapshot {snapshot_name}...")
    
    # Entspricht im Terminal: incus snapshot restore <container_name> <snapshot_name>
    await incus_command("snapshot", "restore", container.name, snapshot_name)
    
    container.log_event("Rollback finished. Container state restored.")
