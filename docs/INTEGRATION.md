# External-link integration contract (planned)

DV owns its transport process, TLS identity, TUN, link addresses and statistics.
A consumer panel owns its client policy routes, NAT, firewall, quotas and traffic
accounting. Neither should silently take over the other's ownership.

The intended WG-panel model is the existing external-link / Easy Mesh approach:
attach an already provisioned link, without DV requiring any panel installation.
Before implementation, inspect the actual Easy Mesh routing, teardown and quota
contracts in the panel repository. This is a design target, not implemented
attachment or auto-discovery.

A future explicit local attachment record should identify schema version, stable
link ID, local interface, local/peer IPs, MTU, endpoint role and statistics path.
It must contain no PEM key, shared token or remote command. Validate interface
ownership and addresses locally, require explicit operator consent for attachment,
and probe reachability; interface existence or UP alone is insufficient.

Detaching must remove only consumer-owned routes/rules and leave DV and other
links intact. DV shutdown must not remove panel-owned interfaces or policies.
No discovery protocol may execute peer-supplied commands or trust remote route
announcements. Automatic scanning and live client migration are out of scope.

Other panels and services may use the IP link through their own explicit routing.
Their product-specific integrations have not been implemented or validated.
No panel dependency is permitted in the transport core.
