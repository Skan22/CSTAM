#!/bin/sh
# Fetches or builds the binaries the chaos and integration tests run for real, into
# ${IPO_LAB_CACHE:-~/.cache/ipo-lab}. Needs curl, tar, gcc, make, autoconf, automake, libtool and
# the libnl3 and openssl development headers (Arch: libnl, openssl, iptables).
set -eu
cache="${IPO_LAB_CACHE:-$HOME/.cache/ipo-lab}"
traefik_version="${TRAEFIK_VERSION:-3.7.13}"
keepalived_version="${KEEPALIVED_VERSION:-2.4.3}"
mkdir -p "$cache"
cd "$cache"

if [ ! -x traefik ]; then
  curl -fsSL -o traefik.tgz \
    "https://github.com/traefik/traefik/releases/download/v${traefik_version}/traefik_v${traefik_version}_linux_amd64.tar.gz"
  tar xzf traefik.tgz traefik
  rm -f traefik.tgz
fi

if [ ! -x "keepalived-${keepalived_version}/bin/keepalived" ]; then
  curl -fsSL -o keepalived.tgz \
    "https://github.com/acassen/keepalived/archive/refs/tags/v${keepalived_version}.tar.gz"
  tar xzf keepalived.tgz
  rm -f keepalived.tgz
  (cd "keepalived-${keepalived_version}" && ./autogen.sh && ./configure && make -j"$(nproc)")
fi

echo "traefik:    $cache/traefik"
echo "keepalived: $cache/keepalived-${keepalived_version}/bin/keepalived"

# `fetch-tools.sh validators`: the exact versions the Quadlets and roles pin, so tests/deploy can
# check every rendered config with the program that will read it.
[ "${1:-}" = validators ] || exit 0
fetch() {  # name url member...
  name="$1"; url="$2"; shift 2
  [ -x "validators/$name" ] && return 0
  mkdir -p validators && tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/archive" "$url"
  case "$url" in
    *.zip) python3 -m zipfile -e "$tmp/archive" "$tmp/x" ;;
    *) mkdir "$tmp/x" && tar xzf "$tmp/archive" -C "$tmp/x" ;;
  esac
  for member in "$@"; do
    install -m 0755 "$(find "$tmp/x" -type f -name "$member" | head -n1)" "validators/${member%-linux-amd64}"
  done
  rm -rf "$tmp"
}
gh=https://github.com
fetch loki "$gh/grafana/loki/releases/download/v3.7.8/loki-linux-amd64.zip" loki-linux-amd64
fetch amtool "$gh/prometheus/alertmanager/releases/download/v0.34.1/alertmanager-0.34.1.linux-amd64.tar.gz" amtool alertmanager
fetch tempo "$gh/grafana/tempo/releases/download/v2.10.8/tempo_2.10.8_linux_amd64.tar.gz" tempo
fetch alloy "$gh/grafana/alloy/releases/download/v1.20.1/alloy-linux-amd64.zip" alloy-linux-amd64
fetch caddy "$gh/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_linux_amd64.tar.gz" caddy
# Podman's Quadlet generator from Debian 13, the Podman the hosts run: it parses the units
# without needing Podman itself.
if [ ! -x validators/quadlet ]; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/podman.deb" \
    https://deb.debian.org/debian/pool/main/p/podman/podman_5.4.2+ds1-2+b2_amd64.deb
  python3 - "$tmp/podman.deb" validators/quadlet <<'PY'
import io, sys, tarfile
data = open(sys.argv[1], "rb").read()
pos = 8  # an ar archive: "!<arch>\n", then 60-byte member headers
while pos < len(data):
    name, size = data[pos:pos + 16].decode().strip(), int(data[pos + 48:pos + 58])
    body = data[pos + 60:pos + 60 + size]
    if name.startswith("data.tar"):
        with tarfile.open(fileobj=io.BytesIO(body)) as t:
            member = t.extractfile("./usr/libexec/podman/quadlet")
            open(sys.argv[2], "wb").write(member.read())
    pos += 60 + size + size % 2
PY
  chmod 0755 validators/quadlet
  rm -rf "$tmp"
fi
ls -l "$cache/validators"
