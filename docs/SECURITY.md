# Security and operating boundaries

- Real TLS certificate and hostname verification plus token authentication; no
  insecure-skip-verification option. Only the listener gets the private TLS key.
- TUN is a privileged raw-IP transport. Incoming source addresses are checked
  against explicit `allowed_sources`. Keep narrow prefixes; do not allow all.
- One authenticated WSS session; bounded associations, packets, bytes and age.
  Diagnostic snapshots are individually atomic counters, not frozen transactions.
- Wire input cannot install routes, execute commands or select arbitrary UDP
  targets. TUN addressing is an explicit local operator decision.
- Generated certificates last seven days. Regenerate the pair on expiry rather
  than disabling verification to hide a certificate error.
- Go TLS/WSS may be fingerprinted. HTML fallback, SNI or padding is not browser
  impersonation, DPI proof or a guarantee against blocking.
- The explicit Ubuntu installer/menu manages only DV-owned paths and services.
  No background upgrade daemon, public metrics, remote administration or automatic
  production integration is included. Read UPDATE.md for root/update trust limits.
- Never publish generated configurations, private keys or tokens. Acceptance
  archives contain only whitelisted test measurements, not carrier logs/configs.
- Test traffic shares production resources unless the operator isolates servers.
  Bounded traffic and safety guards cannot guarantee zero production impact.
