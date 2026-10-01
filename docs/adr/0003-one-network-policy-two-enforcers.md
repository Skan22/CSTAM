# 3. One network policy, enforced by the cloud and by every host

## Status

Accepted.

## Context

The plan lists the security groups in prose and asks for "nftables mirroring the security
groups" on every host. Kept by hand, the two drift, and nothing would show which one a sandbox
is actually stopped by.

## Decision

`security-groups.yaml` (with a JSON Schema and cross-reference checks in
`ansible/filter_plugins/ipo_net.py`) is the only place rules are written. Pulumi turns it into
Neutron security groups, one per group and network, with remote-group references
(`infra/pulumi/ipo_infra/rules.py`); the `hardening` role renders it as nftables with each rule
pinned to the host's own address on that network; the isolation lab builds both from it and
probes every path, with each layer on its own.

## Consequences

- A rule is added once, and the isolation suite fails until something probes it.
- Testing each layer alone showed what each one is for: sandbox-to-sandbox isolation and
  anti-spoofing rest on Neutron alone (teams own their images), and the weak-host path rests on
  the host firewall alone. Both stay on, and the Pulumi policy pack refuses port security off.
- Neutron groups are named `<group>.<network>`, which the control plane (sandbox ports) and the
  bake playbook must use; a test checks they exist.
