#!/usr/bin/env bash
# Nightly job (plan M7): the isolation probes against the `dev` deployment. Run from a CI runner
# outside the platform, with WireGuard up so the bastion is reachable.
#
#   IPO_FLOATING_IP   floating IP of the VIP           IPO_BASTION_IP   floating IP of the bastion
#   IPO_PROBE_SSH     ssh command reaching a test sandbox in the pool, e.g.
#                     "ssh -J ci@10.30.0.10 probe@10.20.0.108"
#   IPO_PEER_SANDBOX  address of a second test sandbox
#
# The two test sandboxes are ordinary pool VMs leased to an `isolation` team, so they sit in
# sg-sandbox with port security on like any other. Exit status is non-zero on any leak.
set -euo pipefail
cd "$(dirname "$0")/../tests"
: "${IPO_FLOATING_IP:?}" "${IPO_BASTION_IP:?}" "${IPO_PROBE_SSH:?}" "${IPO_PEER_SANDBOX:?}"
uv sync --locked
uv run python -m isolation.real --as internet --floating-ip "$IPO_FLOATING_IP" \
  --bastion-ip "$IPO_BASTION_IP"
uv run python -m isolation.real --as sandbox --peer "$IPO_PEER_SANDBOX" --ssh "$IPO_PROBE_SSH"
