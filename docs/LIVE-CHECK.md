# Mixed traffic acceptance of an already running link

`tools/live_check.py server` (listener/B) and `client` (dialer/A) use an existing
managed instance `main`, interface wft0 and 10.254.253.2 / .1 by default. Optional
--instance, --iface, --local and --peer flags select another explicit RFC1918 /30.
Start server first, leave it open at SERVER_READY, then run client. Output is JSON
and one ZIP in a unique per-run folder under /root/dv-live-check-results,
plus atomic latest-client.zip / latest-server.zip aliases for easy retrieval. No package
upgrade is required. This test is separate from the interactive menu.

The runner first verifies the exact service is active, fresh connected statistics,
the local /30 interface address and a peer route via that interface. It binds its
application listeners to the TUN address only (TCP18081/UDP18082), accepts only the
configured peer address, and never starts/stops/restarts a carrier, creates/deletes
an interface, imports configs, edits routes/firewall or reads TLS keys/tokens.

Four paced 256KiB TCP uploads/downloads overlap four-socket UDP echo probes initiated
from BOTH endpoints (2000 datagrams per sender, 1200-byte UDP payload, requested
1.92 Mbps per initiating sender for about ten seconds). Echo returns and TCP add
to total link load. TCP senders share a 2 Mbps payload pacer per endpoint. This
is functional payload acceptance, not achieved high-rate capacity, fine latency,
forwarded-client Internet access or product-specific panel integration.

The helper is pinned to an immutable source commit and SHA256, checked before
loading; only service/traffic helper methods are used, not its carrier runner.
Whole-host CPU85% twice / available memory192MiB, UDP no-valid-reply5s and bounded
socket/server deadlines limit risk but cannot guarantee production is unaffected.

Both ends record whitelisted counters and service MainPID before/after. Counters
include all carrier traffic and cannot be mapped to individual application losses.
The server finish reply is a checkpoint before its own test-socket cleanup; its
separate final report confirms that cleanup. A client pass does not prove peer
cleanup. The service remains running after either checker exits. Reports contain
neither private config nor payload captures; share the generated report ZIPs.

Checker v2 also preserves safe transport_profile and per-lane snapshots and
calculates per-lane counter deltas when the runtime provides them. Cumulative
max_write_us is labeled since-start, never subtracted. Loads and helper are unchanged.
