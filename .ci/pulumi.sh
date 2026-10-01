#!/usr/bin/env bash
# `pulumi.sh preview` on every pull request (its diff goes on the PR), `pulumi.sh up` on dev after
# a merge to main. Needs PULUMI_BACKEND_URL, PULUMI_CONFIG_PASSPHRASE and OS_CLOUD (clouds.yaml).
set -euo pipefail
cd "$(dirname "$0")/../infra/pulumi"
stack="${IPO_STACK:-dev}"
case "${1:?preview or up}" in
  preview) pulumi preview --stack "$stack" --policy-pack policy --diff ;;
  up) pulumi up --stack "$stack" --policy-pack policy --yes --skip-preview
      mkdir -p outputs && pulumi stack output --stack "$stack" --json > "outputs/$stack.json" ;;
  *) echo "usage: $0 preview|up" >&2; exit 2 ;;
esac
