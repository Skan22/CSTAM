# Promote the replica

When the Postgres primary (cp-1 by default) is lost and will not be back soon.

1. Confirm it is the database, not the host's network: `ssh cp-1` through the bastion,
   `systemctl --user -M ipo@ status postgres`. The API's `/readyz` fails on both replicas.
2. Promote and repoint (fences the old primary first if it still answers):
   `ansible-playbook playbooks/promote_replica.yml -e @secrets.yaml -e old_primary=cp-1 -e new_primary=cp-2`
3. Set `ipo_db_primary: cp-2` in `ansible/inventory/group_vars/all.yml` and commit, so the next
   `site.yml` does not point the control plane back.
4. Check: `/readyz` is 200 on both fronts; the dashboard's Overview loads; a registration succeeds.
5. Rebuild the old primary as the new replica once it is healthy: on cp-1, stop Postgres,
   `podman volume rm postgres`, then run `control_plane.yml`. The role seeds an empty volume
   with `pg_basebackup` from the primary. Nightly dumps move with the primary.

Untested: written against the playbook, which has not been run on real hosts.
