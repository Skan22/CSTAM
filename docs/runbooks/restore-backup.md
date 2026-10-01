# Restore the database from a backup

Nightly custom-format dumps go to `~ipo/.local/share/ipo/backups` on the primary (three kept)
and to object storage (`IPO_BACKUP_REMOTE`, e.g. `swift:ipo-backups/dev`).

1. Stop both control planes so nothing writes:
   `systemctl --user -M ipo@ stop ipo-control-plane` on cp-1 and cp-2.
2. Fetch a dump to the primary if it is not there: `rclone copy swift:ipo-backups/dev/<file> ~/backups`.
3. Restore into a fresh database:
   `podman exec -i postgres pg_restore -U postgres --clean --if-exists --create -d postgres < <file>`.
4. Start both control planes. The reconciler repairs anything the cloud changed since the dump
   (orphan ports, VMs, routes) within a cycle.

Untested: the dump and upload units have been checked by Podman's Quadlet generator only.
