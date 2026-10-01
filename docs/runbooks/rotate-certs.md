# Rotate certificates

Host certificates come from step-ca on cp-1 and live 24 hours; `ipo-cert-renew.timer` renews each
one when 8 hours are left and restarts what uses it (ipo-agent on the gateways, the API and its
front on the control plane).

- **Renew everything now** (after a CA change, or to test): `ansible-playbook playbooks/rotate_certs.yml`.
- **A host's timer is failing:** `journalctl -u ipo-cert-renew` on it. A certificate that has
  expired cannot renew itself: delete `/etc/ipo/pki/host.crt` and run its play again; the
  `step_ca` role enrolls it with a fresh one-time token.
- **Check one:** `step certificate inspect /etc/ipo/pki/host.crt --short`.

The agents accept the control plane by its certificate's common name (`ipo-control-plane`), so a
control-plane certificate must keep that name.
