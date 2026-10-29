# TCP-only reference review

Scope: operator-supplied source archives, inspected statically; no reference code,
installers or binaries were executed, copied, translated or bundled. Archive names
are provenance labels, not verification of an upstream tag. No UDP/KCP/QUIC/raw-packet
transport is adopted. Carrying inner UDP over outer TCP remains in scope.

## Findings

- **rathole** (`rathole-dev-latest.zip`, Apache-2.0):
  `src/server.rs` caches TCP data channels (TCP_POOL_SIZE=8) and allocates channels
  to incoming visitors; `src/client.rs` relays bidirectionally;
  `src/transport/tcp.rs` applies configurable socket options. Useful general
  principle: independent TCP connections, bounded waits, explicit channel lifecycle.
  Its per-visitor port forwarding is NOT a drop-in implementation of our raw-IP TUN.
  Reference: https://github.com/rathole-org/rathole
- **BackPack** (`BackPack-1.8.5.zip`, AGPL-3.0): TCP transport, TCPMUX/smux and
  connection-pool code in `internal/client/transport/{tcp,tcpmux,poolmaintain}.go`
  and `internal/utils/network/tcp_dialer.go`. General concepts only: bounded
  parallel connections, per-generation cancellation, negotiated compatibility,
  dial timeout/backoff. No implementation reused; no new dependency added.
  A mux over ONE TCP connection still shares that connection's loss HOL.
  Reference: https://github.com/AminMGMT/BackPack
- **KCP_tunnel** (`KCP_tunnel-main.zip`, GPL-3.0): this archive contains a
  Python/shell installer and documentation, NOT the tunnel engine source.
  TCP menu branches invoke an external kcptun binary with `--tcp`, smux buffer
  flags and sometimes `--conn 7`. This is not evidence that its engine is a
  conventional kernel-TCP byte stream; source for that binary is absent here.
  Large buffer values are not transplanted and no installer is run.
- **Kariz** (`Kariz-main.zip`, custom source-available license): TCP transport and
  session management were read for review only. LICENSE explicitly prohibits use,
  derivation, porting and external integration of its core. None of its algorithms,
  protocol, configuration format or implementation is adopted. No compatibility
  with Kariz is implemented. Reference: https://github.com/Erfan-XRay/Kariz

## Independent DV experiment

The optional two-connection isolation prototype was already implemented locally
before these archives were received/read. It is our existing WSS encoding on two
negotiated connections: TCP/unknown-IP bulk and UDP/ICMP datagrams. It is not a
port of any reference mux, and no foreign wire format is introduced. The review
supports evaluating connection isolation rather than blindly growing one FIFO;
it does not establish that two lanes solve the WAN problem or reach line rate.

Future per-flow TCP connection pools require flow-affinity, fragment handling,
resource bounds and recovery tests. They are not silently added in this preview.
