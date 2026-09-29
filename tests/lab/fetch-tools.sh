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
