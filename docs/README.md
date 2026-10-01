# Documentation

Start here, then follow the section you need.

## The three submission documents

| | |
| --- | --- |
| [System architecture](architecture/system-architecture.md) | what the platform is, every component, the networks, the design decisions, limits, and what is verified |
| [OpenStack high availability](architecture/openstack-ha.md) | how failover works on OpenStack, measured results, every failure mode, split brain, findings from the real cloud |
| [Gateway data flow](architecture/gateway-data-flow.md) | the diagram and a step-by-step walk of the request, configuration and telemetry paths |

## Architecture

| Document | Contents |
| --- | --- |
| [Control plane](architecture/control-plane.md) | data model, lease state machine, registration and teardown sagas, controllers, reconciler rules, API, auth |
| [Security](architecture/security.md) | trust boundaries, threat and defence table, secrets and key handling |
| [Decisions (ADRs)](adr) | [2 lease state machine](adr/0002-lease-state-machine.md), [3 one policy, two enforcers](adr/0003-one-network-policy-two-enforcers.md), [4 rootless Quadlets](adr/0004-rootless-quadlets-on-the-host-network.md) |

## Guides

| Guide | For |
| --- | --- |
| [Getting started](guides/getting-started.md) | running everything on a laptop |
| [Deployment](guides/deployment.md) | Pulumi, Ansible and Quadlets onto OpenStack |
| [Configuration](guides/configuration.md) | `platform.yaml`, `security-groups.yaml`, every environment variable |
| [Operations](guides/operations.md) | health, metrics, alerts, routine tasks |
| [Testing](guides/testing.md) | the suites, the labs, results, and what real software found |

## Reference

| | |
| --- | --- |
| API | [OpenAPI](api/openapi.json), [Postman collection](api/ipo.postman_collection.json) |
| Runbooks | [alerts](runbooks/alerts.md), [manual failover](runbooks/manual-failover.md), [promote replica](runbooks/promote-replica.md), [rotate certificates](runbooks/rotate-certs.md), [rebuild from zero](runbooks/rebuild-from-zero.md), [restore backup](runbooks/restore-backup.md) |
| Reports | [isolation results](reports/isolation.md), [FelCloud survey](reports/felcloud-survey.md) |
| Components | [gateway agent](../gateway-agent/README.md), [dashboard](../dashboard/README.md), [Ansible](../ansible/README.md), [Quadlets](../quadlets/README.md), [Pulumi](../infra/pulumi/README.md), [system tests](../tests/README.md) |
| Diagrams | sources and images in [`diagrams/`](diagrams) |

For the submission checklist and the list of gaps, see [SUBMISSION.md](SUBMISSION.md).
