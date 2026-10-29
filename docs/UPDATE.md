# Ubuntu menu, installation and transactional updates

This is an experimental preview, not production certification. Supported hosts:
Ubuntu 22.04/24.04 with systemd, Python 3, iproute2, ping, TUN and amd64/arm64. curl is
needed only for the optional bootstrap download. No package is installed silently.

## Install the menu (does not import, start or enable a tunnel)

The repository and release assets must be accessible publicly. A private GitHub
repository returns an error to this anonymous installer; never put a PAT in a URL.
Inspect the downloaded script before running it as root.

```bash
installer="$(mktemp)"
curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  https://raw.githubusercontent.com/dalmaktube-creator/dv-tunnel/v0.1.0-preview.1/tools/dvctl.py \
  -o "$installer" &&
sudo python3 "$installer" install --version v0.1.0-preview.1
rm -f "$installer"
sudo dv-tunnel
```

Wait for the `build-preview-release` Actions job and the release assets before
installing. Merely committing the workflow does not mean a release was built.
The command `dv-tunnel` opens the menu; `dv-tunnel status` prints version/health.
The transport executable remains at `/opt/dv-tunnel/current/dv-tunnel` so the
management command is not confused with the raw transport CLI.

## Explicitly import one existing pair side

Choose one instance name. The source directory's config is read and copied, not
edited or published. Import creates only private managed files and never starts
or enables the service. It requires a valid raw-IP TUN config with the exact peer
/32 allowlist. A new pair can be generated with `tools/dv_pair.py` as in README.

```bash
# Server A example; local addresses must match the actual pair
sudo dv-tunnel import main --field-dir /path/to/node-a \
  --local 10.254.253.1/30 --peer 10.254.253.2
sudo dv-tunnel start main
sudo dv-tunnel status
# Optional, explicit boot persistence (not done by import/install):
sudo systemctl enable dv-tunnel@main.service
```

On B, import its own node-b config using local .2 and peer .1. Never start two
carriers using the same interface or port. Existing field tests must finish
first; the runner refuses an occupied interface rather than taking it over.
No installer imports old test files automatically or migrates live panel links.
For private diagnostics use `journalctl -u dv-tunnel@main`; review before sharing.

## Update and rollback

Menu actions show active instance names and require typing `RESTART` before any
update/rollback interruption. CLI equivalents are explicit:

```bash
sudo dv-tunnel check-update
sudo dv-tunnel update                  # refuses if managed tunnels are active
sudo dv-tunnel update --restart        # explicitly authorizes a brief interruption
sudo dv-tunnel rollback --restart      # previous locally retained runtime
sudo dv-tunnel stop main --yes
sudo dv-tunnel restart main --yes
```

Preview releases are included in update selection, and the menu says Preview.
No background updater/timer or unattended restart is installed. For a specific
published version use `update --version vX.Y.Z-preview.N`; downgrades use local
rollback, not an accidental older network release.

1. Root-only exclusive lock, strict version/architecture validation and protected
   managed paths. Refuse an unrelated `/usr/local/bin/dv-tunnel` or service unit.
2. Download one architecture's immutable versioned release and SHA256SUMS over
   HTTPS from the fixed repository. Verify the checksum before unpacking. Flat
   file whitelist, no symlinks/hardlinks/traversal, compressed/decompressed size
   limits, Python syntax and actual binary version checks.
3. Validate every managed config with the candidate binary's `--check` before
   touching the current pointer. Secrets remain under `/etc/dv-tunnel/instances`.
4. Atomically switch the current symlink; restart only previously active, explicitly
   managed instances if authorized. No previously stopped instance is started.
5. Require fresh stats from the candidate version, a connected WSS session AND
   a successful ICMP probe of the peer bound to the TUN, not just `systemctl active`. If it fails, restore the old pointer, restart only
   affected managed instances and recheck their connected health. Report recovery
   failures honestly, requiring manual inspection.
6. Keep the previous local runtime for rollback. No configuration migration, archive
   pruning or service-unit replacement is performed during updates.

Checksum plus HTTPS is not an independent publisher signature: GitHub/repository
access remains part of the trust boundary. Build from reviewed source if this is
insufficient. No claim of signed provenance, perfect uptime or universally safe
rollback is made. Peers must remain wire-compatible; update one side at a time.
A down/unreachable peer or blocked peer ICMP causes the health gate to fail.
This probes the link, not every routed application or Internet destination.

Paths: `/opt/dv-tunnel/releases/<version>` and current/previous symlinks; private
`/etc/dv-tunnel/instances/<name>`; `/var/lib/dv-tunnel/<name>/stats.json`; exactly
one managed `dv-tunnel@.service` template. Neither Moory nor existing panel files,
NAT, firewall, policy/default routing, DNS or other service units are modified.
The service creates only its own nonpersistent TUN and connected /30 route.

## Publishing previews (maintainer)

Bump VERSION and push main. The release workflow runs Go race/vet, configuration,
manager and concurrent payload tests, builds Linux amd64/arm64, emits exactly the
whitelisted runtime files and checksums, then creates a prerelease/tag at that
commit. It refuses to overwrite an existing release. A red Actions job is not a
release; fix it before recommending installation. Stable production publishing
is deliberately not enabled by this preview-only workflow.
