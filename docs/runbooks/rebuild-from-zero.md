# Rebuild from zero

1. `cd infra/pulumi && pulumi up --policy-pack policy` (stack `dev` or `demo`), then
   `pulumi stack output --json > outputs/<stack>.json`.
2. Put the bastion's floating IP in your WireGuard peer config; add your peer to `bastion_peers`.
3. `cd ansible && sops -d secrets.enc.yaml > secrets.yaml`.
4. Build the artifacts CI normally provides: `gateway-agent/dist/ipo-agent`, `dashboard/dist/`,
   and the control-plane image in the registry (`podman build -f control-plane/Containerfile .`).
5. `ansible-playbook playbooks/bake_image.yml -e @secrets.yaml -e image_version=N` if the
   sandbox image does not exist yet.
6. `ansible-playbook playbooks/site.yml -e @secrets.yaml`. Run it twice: the second run should
   change nothing.
7. Restore data if this replaces a running platform (restore-backup.md), then run the isolation
   probes: `.ci/isolation-dev.sh`.
