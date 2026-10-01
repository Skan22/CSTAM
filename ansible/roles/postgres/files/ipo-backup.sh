#!/bin/sh
# Managed by Ansible (role postgres): one custom-format dump, keeping the last three locally.
set -eu
f="/backups/ipo-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump -Fc -f "$f.part"
mv "$f.part" "$f"
ls -1t /backups/*.dump | tail -n +4 | xargs -r rm --
