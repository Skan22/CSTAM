"""Each role's templates rendered for each host the way Ansible would, then read by the program
that reads them in production, at the version the deploy pins. A test skips when its program is
missing (`sh lab/fetch-tools.sh validators` fetches them)."""

import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploy.conftest import hostvars, tool
from lab import render, tools

PLATFORM_HOSTS = list(render.platform()["network"]["hosts"])
GATEWAYS = ["gw-a", "gw-b"]
OBS = render.REPO / "observability"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    assert p.returncode == 0, f"{' '.join(cmd)}\n{p.stdout}\n{p.stderr}"
    return p


def in_netns(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    if not tools.can_unshare():
        pytest.skip("needs unprivileged user namespaces")
    return run(["unshare", "--user", "--map-root-user", "--net", *cmd], **kw)


# ------------------------------------------------------------------ hardening


@pytest.mark.parametrize("host", PLATFORM_HOSTS)
def test_the_firewall_loads(host: str, tmp_path: Path) -> None:
    v = hostvars(host, "hardening", "bastion")
    rules = render.render("hardening", "nftables.conf.j2", **v)
    included = tmp_path / "nftables.d"
    included.mkdir()
    if host == "bastion":  # the bastion's own table, included by the main file
        write(included / "bastion.nft", render.render("bastion", "bastion.nft.j2", **v))
    rules = rules.replace('include "/etc/nftables.d/*.nft"', f'include "{included}/*.nft"')
    conf = write(tmp_path / "nftables.conf", rules)
    in_netns([str(tool("nft")), "-c", "-f", str(conf)])
    # Loaded twice in a row, as a reload does: the table is replaced, not duplicated.
    in_netns(["sh", "-c", f"nft -f {conf} && nft -f {conf} && nft list tables | grep -c 'inet ipo$'"])


@pytest.mark.parametrize("host", PLATFORM_HOSTS)
def test_every_sysctl_exists(host: str) -> None:
    text = render.render("hardening", "sysctl.conf.j2", **hostvars(host, "hardening"))
    keys = [ln.split("=")[0].strip() for ln in text.splitlines() if "=" in ln and not ln.startswith("#")]
    assert "net.ipv4.ip_forward" in keys
    paths = " ".join("/proc/sys/" + k.replace(".", "/") for k in keys)
    in_netns(["sh", "-c", f"for p in {paths}; do test -e $p || {{ echo missing $p; exit 1; }}; done"])


def test_only_the_bastion_forwards_and_only_gateways_bind_low_ports() -> None:
    for host in PLATFORM_HOSTS:
        text = render.render("hardening", "sysctl.conf.j2", **hostvars(host, "hardening"))
        assert ("net.ipv4.ip_forward = 1" in text) == (host == "bastion"), host
        assert ("ip_unprivileged_port_start = 80" in text) == (host in GATEWAYS), host


def test_sshd_accepts_the_drop_in(tmp_path: Path) -> None:
    key = tmp_path / "hostkey"
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    v = hostvars("cp-1", "hardening", hardening_ssh_allow_users=["alice", "bob"])
    conf = write(tmp_path / "sshd.conf", render.render("hardening", "sshd_ipo.conf.j2", **v))
    run([str(tool("sshd")), "-t", "-f", str(conf), "-h", str(key)])


# ------------------------------------------------------------------ gateways


@pytest.mark.parametrize("host", GATEWAYS)
def test_keepalived_accepts_its_config(host: str, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for f in ("ipo-check-traefik", "ipo-vrrp-notify"):
        shutil.copy(render.ROLES / "keepalived" / "files" / f, bin_dir / f)
    state = tmp_path / "run"  # what the role's tmpfiles entry creates at boot, before keepalived
    state.mkdir()
    v = hostvars(host, "keepalived", keepalived_bin_dir=str(bin_dir), keepalived_script_security=False,
                 keepalived_script_user="root", keepalived_state_file=str(state / "vrrp_state"),
                 keepalived_fault_file=str(state / "fault"))
    text = render.render("keepalived", "keepalived.conf.j2", **v)
    assert f"priority {150 if host == 'gw-a' else 100}" in text
    conf = write(tmp_path / "keepalived.conf", text)
    if not tools.can_unshare():
        pytest.skip("needs unprivileged user namespaces")
    p = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "sh", "-c",
                        f"ip link add {v['keepalived_interface']} type dummy && "
                        f"{tool('keepalived')} --config-test --use-file {conf} 2>&1"],
                       capture_output=True, text=True, timeout=60)
    # In a user namespace / is not owned by root, so the script-security walk up the path can
    # never pass; the role enables it on the real gateways. Anything else is a real complaint.
    complaints = [ln for ln in p.stdout.splitlines() if ln.strip() and "SECURITY VIOLATION" not in ln]
    assert not complaints and p.returncode in (0, 6), p.stdout


def units(host: str, *roles_templates: tuple[str, str], **extra: Any) -> dict[str, str]:
    v = hostvars(host, *{r for r, _ in roles_templates}, "keepalived", **extra)
    return {t.removesuffix(".j2"): render.render(r, t, **v) for r, t in roles_templates}


GATEWAY_UNITS = (("gw_agent", "ipo-agent.service.j2"), ("gateway_network", "ipo-edge-routing.service.j2"),
                 ("step_ca", "ipo-cert-renew.service.j2"), ("step_ca", "ipo-cert-renew.timer.j2"))


CP_UNITS = (("step_ca", "ipo-cert-renew.service.j2"), ("step_ca", "ipo-cert-renew.timer.j2"))


@pytest.mark.parametrize("host,templates", [("gw-a", GATEWAY_UNITS), ("cp-1", CP_UNITS)],
                         ids=["gw-a", "cp-1"])
def test_systemd_accepts_the_units(host: str, templates: tuple[tuple[str, str], ...],
                                   tmp_path: Path) -> None:
    """Executables are pointed at /usr/bin/true: what is checked is every directive."""
    for name, text in units(host, *templates).items():
        text = re.sub(r"^(Exec\w+=-?)/\S+", r"\1/usr/bin/true", text, flags=re.M)
        f = write(tmp_path / name, text)
        p = subprocess.run([str(tool("systemd-analyze")), "verify", "--man=no", str(f)],
                           capture_output=True, text=True)
        noise = [ln for ln in p.stderr.splitlines() if ln.strip() and "is not executable" not in ln]
        assert p.returncode == 0 and not noise, p.stderr


def test_the_agent_unit_is_rated_safe_by_systemd(tmp_path: Path) -> None:
    root = tmp_path / "root"
    text = units("gw-a", ("gw_agent", "ipo-agent.service.j2"))["ipo-agent.service"]
    write(root / "etc/systemd/system/ipo-agent.service", text)
    out = run([str(tool("systemd-analyze")), "security", "--offline=true", f"--root={root}",
               "ipo-agent.service"]).stdout
    score = float(re.search(r"Overall exposure level for ipo-agent.service: ([\d.]+)", out).group(1))  # type: ignore[union-attr]
    assert score <= 2.0, out[-2000:]


def test_the_renewal_unit_reloads_what_uses_the_certificate() -> None:
    gw = units("gw-a", ("step_ca", "ipo-cert-renew.service.j2"))["ipo-cert-renew.service"]
    cp = units("cp-1", ("step_ca", "ipo-cert-renew.service.j2"))["ipo-cert-renew.service"]
    assert "try-restart ipo-agent.service" in gw and "--user" not in gw
    assert "--machine=ipo@ try-restart ipo-control-plane.service ipo-web.service" in cp
    assert "ExecCondition=/usr/local/bin/step certificate needs-renewal" in gw


def test_the_renewal_gate_skips_a_fresh_certificate_and_runs_for_an_old_one(tmp_path: Path) -> None:
    """ExecCondition's contract: exit 1 skips the unit, 0 runs it (step's needs-renewal)."""
    step = tool("step")
    for lifetime, expected in (("24h", 1), ("6h", 0)):
        crt, key = tmp_path / f"{lifetime}.crt", tmp_path / f"{lifetime}.key"
        run([str(step), "certificate", "create", "gw-a", str(crt), str(key), "--profile",
             "self-signed", "--subtle", "--no-password", "--insecure", "--not-after", lifetime])
        rc = subprocess.run([str(step), "certificate", "needs-renewal", str(crt), "--expires-in", "8h"],
                            capture_output=True).returncode
        assert rc == expected, lifetime


# ------------------------------------------------------------------ control plane


def self_signed(tmp_path: Path) -> tuple[Path, Path]:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ipo-control-plane")])
    import ipaddress
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    crt = write(tmp_path / "pki" / "host.crt", cert.public_bytes(serialization.Encoding.PEM).decode())
    k = write(tmp_path / "pki" / "host.key", key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode())
    return crt, k


def test_caddy_accepts_the_front(tmp_path: Path) -> None:
    self_signed(tmp_path)
    v = hostvars("cp-1", "control_plane", ipo_pki_dir=str(tmp_path / "pki"))
    caddyfile = write(tmp_path / "Caddyfile", render.render("control_plane", "Caddyfile.j2", **v))
    run([str(tool("caddy")), "validate", "--config", str(caddyfile), "--adapter", "caddyfile"])


def test_the_users_file_is_what_the_control_plane_accepts() -> None:
    v = hostvars("cp-1", "control_plane")
    users = json.loads(render.render("control_plane", "users.json.j2", **v))
    assert {u["email"] for u in users} == {"agent-gw-a@cstam.felcloud.tn", "agent-gw-b@cstam.felcloud.tn"}
    assert all(set(u) == {"email", "password", "role"} and u["role"] == "operator" for u in users)


def test_the_control_plane_env_points_at_both_agents_and_the_same_origin_grafana() -> None:
    env = render.render("control_plane", "control-plane.env.j2", **hostvars("cp-2", "control_plane"))
    lines = dict(ln.split("=", 1) for ln in env.splitlines() if "=" in ln and not ln.startswith("#"))
    assert lines["IPO_AGENTS"] == "gw-a=https://10.30.0.11:8443,gw-b=https://10.30.0.12:8443"
    assert lines["IPO_GRAFANA_URL"] == "/grafana" and lines["IPO_MIGRATE"] == "0"
    assert "PASSWORD" not in env.replace("IPO_ADMIN_PASSWORD", "") and "SECRET" not in env


# ------------------------------------------------------------------ postgres


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_postgres_starts_on_the_rendered_config_and_the_bootstrap_is_repeatable(tmp_path: Path) -> None:
    initdb, pg_ctl, psql = tool("initdb"), tool("pg_ctl"), tool("psql")
    awkward = "it's a \\ test"  # quotes and backslashes must survive psql's \set
    v = hostvars("cp-1", "postgres", ipo_mgmt_ip="127.0.0.1", postgres_cp_ips=["127.0.0.1"],
                 postgres_app_password=awkward)
    conf_dir = tmp_path / "conf"
    write(conf_dir / "pg_hba.conf", render.render("postgres", "pg_hba.conf.j2", **v))
    conf = render.render("postgres", "postgresql.conf.j2", **v).replace(
        "/etc/ipo/postgres/pg_hba.conf", str(conf_dir / "pg_hba.conf"))
    write(conf_dir / "postgresql.conf", conf)
    data, pw = tmp_path / "data", write(tmp_path / "pw", "postgres-pw")
    run([str(initdb), "-D", str(data), "-U", "postgres", "--auth=scram-sha-256", f"--pwfile={pw}"])
    port = free_port()
    run([str(pg_ctl), "-D", str(data), "-w", "-l", str(tmp_path / "log"), "-o",
         f"-c config_file={conf_dir / 'postgresql.conf'} -p {port} -k {tmp_path}", "start"])
    try:
        env = {**os.environ, "PGPASSWORD": "postgres-pw"}
        sql = render.render("postgres", "bootstrap.sql.j2", **v)
        for _ in range(2):
            run([str(psql), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-f", "-"],
                input=sql, env=env)
        q = run([str(psql), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-tAc",
                 "SELECT rolname, rolreplication FROM pg_roles WHERE rolname IN ('ipo','replicator')"
                 " ORDER BY 1"], env=env).stdout.split()
        assert q == ["ipo|f", "replicator|t"]
        who = run([str(psql), "-h", "127.0.0.1", "-p", str(port), "-U", "ipo", "-d", "ipo", "-tAc",
                   "SELECT current_user"], env={**os.environ, "PGPASSWORD": awkward}).stdout.strip()
        assert who == "ipo"
    finally:
        subprocess.run([str(pg_ctl), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)


# ------------------------------------------------------------------ observability


def obs(tmp_path: Path, **extra: Any) -> dict[str, Path]:
    v = hostvars("cp-2", "observability", **extra)
    files = {}
    for t in ("prometheus/prometheus.yml", "alertmanager/alertmanager.yml", "loki/loki.yml",
              "tempo/tempo.yml", "grafana/grafana.ini", "grafana/provisioning/datasources/ipo.yml",
              "grafana/provisioning/dashboards/ipo.yml"):
        files[t] = write(tmp_path / t, render.render("observability", t + ".j2", **v))
    return files


def test_prometheus_accepts_its_config_and_the_alert_rules(tmp_path: Path) -> None:
    f = obs(tmp_path)["prometheus/prometheus.yml"]
    shutil.copy(OBS / "alerts.yml", tmp_path / "prometheus" / "alerts.yml")
    f.write_text(f.read_text().replace("/etc/prometheus/", f"{tmp_path}/prometheus/"))
    run([str(tool("promtool")), "check", "config", str(f)])
    run([str(tool("promtool")), "check", "rules", str(OBS / "alerts.yml")])


@pytest.mark.parametrize("webhook", ["", "https://hooks.example/ipo"])
def test_alertmanager_accepts_its_config(tmp_path: Path, webhook: str) -> None:
    f = obs(tmp_path, observability_alert_webhook=webhook)["alertmanager/alertmanager.yml"]
    run([str(tool("amtool")), "check-config", str(f)])


def test_loki_accepts_its_config(tmp_path: Path) -> None:
    f = obs(tmp_path)["loki/loki.yml"]
    run([str(tool("loki")), "-verify-config", f"-config.file={f}"])


def test_tempo_accepts_its_config(tmp_path: Path) -> None:
    f = obs(tmp_path)["tempo/tempo.yml"]
    run([str(tool("tempo")), "-config.verify", f"-config.file={f}"])


def test_the_dashboards_grafana_provisions_use_the_datasource_uids_it_defines(tmp_path: Path) -> None:
    ds = yaml.safe_load(obs(tmp_path)["grafana/provisioning/datasources/ipo.yml"].read_text())
    uids = {d["uid"] for d in ds["datasources"]}
    for f in (OBS / "dashboards").glob("*.json"):
        used = set(re.findall(r'"uid": "(\w+)"', json.dumps(
            [p.get("datasource") for p in json.loads(f.read_text())["panels"]])))
        assert used <= uids, f.name


# ------------------------------------------------------------------ alloy


ALLOY_HOSTS = {"gateway": "gw-a", "control_plane": "cp-1", "relay": "relay", "bastion": "bastion",
               "sandbox": "sandbox-image"}


@pytest.mark.parametrize("role", list(ALLOY_HOSTS))
def test_alloy_loads_each_hosts_configuration(role: str, tmp_path: Path) -> None:
    """`alloy run` evaluates every component when it loads the directory, and exits at once if
    one is wrong; one still running after a few seconds has accepted the configuration."""
    alloy = tool("alloy")
    host = ALLOY_HOSTS[role]
    v = hostvars(host, "alloy", "relay")
    assert v["alloy_role"] == role
    conf = tmp_path / "alloy"
    write(conf / "host.alloy", render.render("alloy", "host.alloy.j2", **v))
    if role == "relay":
        write(conf / "relay.alloy", render.render("relay", "relay.alloy.j2", **v))
    for f in conf.iterdir():
        run([str(alloy), "fmt", str(f)])
    relay_ip = render.platform()["network"]["hosts"]["relay"]["sandbox"]
    script = (f"ip link set lo up && ip addr add {relay_ip}/32 dev lo && exec timeout -k 3 8 {alloy} run"
              f" --server.http.listen-addr=127.0.0.1:12345 --storage.path={tmp_path / 'data'}"
              f" --disable-reporting {conf}")
    if not tools.can_unshare():
        pytest.skip("needs unprivileged user namespaces")
    p = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "sh", "-c", script],
                       capture_output=True, text=True, timeout=60)
    # Still running when timeout stopped it: 124 after TERM, or killed with timeout's whole process
    # group when Alloy takes longer than 3 s to flush and stop.
    assert p.returncode in (124, 137, -9), p.stderr[-3000:]

