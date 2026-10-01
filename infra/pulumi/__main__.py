"""`pulumi up`: everything long-lived, from platform.yaml and security-groups.yaml."""

import pulumi

from ipo_infra import program, spec

cfg = pulumi.Config("ipo")
platform, groups = spec.load()
built = program.build(platform, groups, key_pair=cfg.get("keyPair") or "ipo-ops",
                      prefix=cfg.get("prefix") or "", public_key=cfg.get("publicKey") or "",
                      floating_ips=cfg.get_bool("floatingIps") is not False,
                      affinity=cfg.get("antiAffinity") or "soft-anti-affinity")
for name, value in built.outputs().items():
    pulumi.export(name, value)
