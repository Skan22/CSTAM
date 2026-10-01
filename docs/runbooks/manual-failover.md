# Manual failover

Moves the VIP off the gateway that holds it, for maintenance or to test.

- **From the dashboard:** Gateways, "Fail over" (admin), or `POST /v1/gateways/failover`. The
  control plane asks the current MASTER's agent to fault itself (`POST /fault`, which writes the
  keepalived track file). keepalived goes to FAULT and the peer takes the VIP. The chaos suite
  measures this under load at 1.5 s or less of interruption.
- **Undo:** on that gateway, `sudo rm /run/ipo/fault` (what the agent's `DELETE /fault` does; it
  needs the control plane's client certificate). Restarting the agent does not clear it. With
  `nopreempt` the VIP stays where it is; the recovered gateway becomes BACKUP.
- **Without the control plane:** on the MASTER, `systemctl stop keepalived`; the peer takes over
  at once (priority-0 advert). Start it again afterwards.

Check the Gateways page, or the `ipo_gateway_masters` metric: exactly one MASTER.
