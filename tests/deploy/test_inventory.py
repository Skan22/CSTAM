"""The Ansible tree agrees with itself and with platform.yaml, and every reference resolves."""

import re
from pathlib import Path

import yaml

from deploy.conftest import QUADLETS, SECRETS
from lab import render

ANSIBLE = render.ANSIBLE
PLAYBOOKS = sorted((ANSIBLE / "playbooks").glob("*.yml"))
ROLE_DIRS = sorted(p for p in (ANSIBLE / "roles").iterdir() if p.is_dir())


def text_of(*dirs: Path) -> str:
    return "\n".join(p.read_text() for d in dirs for p in d.rglob("*") if p.is_file()
                     and p.suffix in (".yml", ".j2", ".yaml"))


def test_the_static_inventory_is_platform_yaml() -> None:
    plat = render.platform()
    inv = yaml.safe_load((ANSIBLE / "inventory" / "static.yml").read_text())
    hosts = {h: v["ansible_host"] for g in inv["all"]["children"].values() for h, v in g["hosts"].items()}
    assert hosts == {h: a["mgmt"] for h, a in plat["network"]["hosts"].items()}


def test_group_vars_are_for_groups_that_exist() -> None:
    groups = set(render.inventory())
    for f in (ANSIBLE / "inventory" / "group_vars").glob("*.yml"):
        assert f.stem in groups, f"{f.name} matches no inventory group"


def test_every_role_a_playbook_names_exists_and_every_role_is_used() -> None:
    used: set[str] = set()
    for pb in PLAYBOOKS:
        for play in yaml.safe_load(pb.read_text()):
            used.update(r if isinstance(r, str) else r["role"] for r in play.get("roles", []))
    used |= set(re.findall(r"name: (\w+)\n\s+tasks_from:", text_of(ANSIBLE / "roles", ANSIBLE / "playbooks")))
    for meta in (ANSIBLE / "roles").glob("*/meta/main.yml"):
        used.update(d["role"] for d in yaml.safe_load(meta.read_text()).get("dependencies", []))
    present = {p.name for p in ROLE_DIRS}
    assert used <= present, used - present
    assert present <= used, f"roles nothing runs: {present - used}"
    for role in present:
        assert (ANSIBLE / "roles" / role / "tasks" / "main.yml").is_file()


def test_the_secrets_example_has_exactly_the_keys_the_roles_read() -> None:
    read = set(re.findall(r"ipo_secrets\.(\w+)", text_of(ANSIBLE / "roles", ANSIBLE / "inventory",
                                                       ANSIBLE / "playbooks")))
    assert read == set(SECRETS["ipo_secrets"])


def test_every_quadlet_is_installed_by_a_role_and_every_installed_one_exists() -> None:
    roles = text_of(ANSIBLE / "roles")
    # Timers are also systemd's own (podman-auto-update.timer), so only ours count for those.
    named = {n for n in re.findall(r"[\w-]+\.(?:container|volume|timer)\b", roles)
             if not n.endswith(".timer") or (QUADLETS / n).exists()}
    files = {p.name for p in QUADLETS.iterdir() if p.suffix in (".container", ".volume", ".timer")}
    assert named <= files, named - files
    assert files <= named, f"units no role installs: {files - named}"


def test_every_secret_a_quadlet_mounts_is_created_by_a_role() -> None:
    mounted = {line.split("=", 1)[1].split(",")[0] for p in QUADLETS.glob("*.container")
               for line in p.read_text().splitlines() if line.startswith("Secret=")}
    created = set(re.findall(r"(?:podman_secret_name|name): (ipo-[\w-]+)", text_of(ANSIBLE / "roles")))
    assert mounted <= created, mounted - created
