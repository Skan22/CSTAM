#!/bin/sh
# Managed by Ansible (role postgres): copy the dumps to object storage.
set -eu
: "${IPO_BACKUP_REMOTE:?set it in backup.env}"
exec rclone copy /backups "$IPO_BACKUP_REMOTE" --include '*.dump'
