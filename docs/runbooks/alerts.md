# Alert runbook

Each section matches an alert in `observability/alerts.yml`. None of these has been drilled
against a live deployment yet.

## IPOGatewayVersionSkew
The gateways report different `ipo_config_live_version`. Check `GET /v1/gateways` for the lagging
one and its last heartbeat. The reconciler repushes the newest pending or live version after its
grace period; if the gateway keeps refusing it, read the agent's `/status` and logs by trace id.

## IPOConfigRolledBack
The agent applied a config, failed convergence or a probe, and restored its last known good one.
The control plane marks that version `rejected`. Find the route that failed the probe (agent
logs, `stage=probe`); one dead backend blocks other route changes until it is fixed or removed.

## IPOConfigSignatureRejected
A push failed signature verification. Either the signing key differs between control plane and
agent (`IPO_SIGNING_PUBLIC_KEY`) or something other than the control plane is pushing. Treat as a
security event until the first is ruled out.

## IPOGatewayFlapping
More than three VRRP transitions in two minutes. Check keepalived on both gateways, the unicast
peer addresses and the fault track file (`DELETE /fault` clears an injected fault).

## IPOGatewaySplitBrain
Both gateways report MASTER, so both hold the VIP and clients reach whichever answers their ARP
request first. VRRP normally resolves this in a few adverts once the peers hear each other again;
if it persists they cannot: check the peer's unicast address, that IP protocol 112 is allowed
between the two edge ports (a security group or nftables rule), and `journalctl -u keepalived` on
both. To stop the damage now, `POST /v1/gateways/failover` faults the gateway the control plane
lists first, or `systemctl stop keepalived` on the one you want to give up. `ipo_gateway_masters`
counts only gateways that reported after the newest promotion, so a gateway that died as MASTER
does not raise this alert.

## IPONoGatewayMaster
Neither gateway holds the VIP. Read each agent's `GET /status`: `FAULT` on both means the
Traefik health check or the fault file is failing everywhere (check Traefik's `/ping`, then
`DELETE /fault`); `BACKUP` on both means keepalived cannot elect one, which usually means a
misconfigured `unicast_peer` or a `nopreempt` pair started in the same state after a reboot.

## IPOWarmPoolLow
Fewer warm VMs than half the target. Check `ipo_saga_jobs_total` and the pool manager; a quota
error from OpenStack is the usual cause.

## IPOFreeAddressesLow
The sandbox range is nearly exhausted. Look at `ipo_ip_addresses` by state: many `quarantined`
addresses means recent teardowns; many `leased` means teams that need tearing down.

## IPOSagaJobsFailing
Read `jobs.last_error` and `saga_steps` for the failed job. A compensated registration left the
team `failed` with its resources released; a `failed` job needs a person (an undo step failed).

## IPOReconcilerRepairing
Read `ipo_reconciler_repairs_total{rule}`. `orphan_vm`/`orphan_port` mean a saga leaked cloud
resources, `vm_gone` means a VM disappeared under a live team, `gateway_behind` means a push was
lost.

## IPOProvisioningSlow
Compare `ipo_provision_duration_seconds` by `path` and the per-step `ipo_saga_step_duration_seconds`
to find the slow step; `converge` and `probe` include gateway reload time.
