# Pulumi (Day 0)

Everything long-lived, from `platform.yaml` and `security-groups.yaml` (both schema-validated, and
the second cross-checked, before anything is created): the three networks and their subnets, the
two routers, the security groups, the VIP port with its floating IP, and the VMs as four
components (`GatewayPair`, `ControlPlane`, `Relay`, `Bastion`).

```
pulumi login <state backend>          # Swift (S3 API) bucket or a file backend on the bastion
pulumi stack init dev --secrets-provider passphrase
pulumi preview --policy-pack policy   # what CI runs on every pull request
pulumi up --policy-pack policy        # what CI runs on dev after a merge
pulumi stack output --json > outputs/dev.json   # read by Ansible as ipo_outputs
```

**Security groups.** Neutron applies groups per port, the spec scopes rules per network, so each
spec group becomes one Neutron group per network it has ports on, named `<group>.<network>`
(`sg-gateway.edge`, `sg-sandbox.sandbox`), and every port gets the one for its network. A rule from
a group references all of that group's network groups (any address of a member), never an
address range. This is what `tests/isolation` emulates, and `tests/test_rules.py` rebuilds the spec
from the generated rules to prove nothing was added or lost.

**Policy pack** (`policy/`, CrossGuard, mandatory): nothing open to 0.0.0.0/0 but 80 and 443 on
the gateways and WireGuard on the bastion (the plan lists only the first; without the second no
operator gets in); port security on for every port and network; a `role` tag on every VM;
no DHCP on sandbox-net.

**Tests** (`uv run pytest`): the rules module against the spec, every guardrail, and the whole
program under Pulumi's mocks, with each resource it would create passed through the policy pack.
Neither `pulumi preview` nor `up` has been run: no Pulumi CLI or OpenStack project was available
where this was written.
