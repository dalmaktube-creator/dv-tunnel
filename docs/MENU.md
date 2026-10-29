# Terminal dashboard

Run `sudo dv-tunnel` for the interactive Ubuntu/SSH menu. Preview 2 adds a cyan /
magenta dashboard, current and rollback versions, node/architecture, managed-link
counts and fresh connection state. Wide terminals use a compact two-column menu;
narrow terminals wrap to one column without new Python packages.

Safety semantics are unchanged: live update/rollback still requires `RESTART`,
and stop/restart still requires `YES`. Start is always an explicit action.
Successful version switching exits the old menu: reopen `dv-tunnel` to use the
newly installed manager. Installation never imports or starts a link automatically.

`CONNECTED` means the service is active and its statistics are less than six
seconds old with `connected=1`. It is not full routed-application acceptance.
Missing/stale stats show `STALE / UNKNOWN`; inactive services never look connected
because of an old statistics file. Existing field probes are not managed instances.

- `NO_COLOR=1 dv-tunnel`: disable ANSI colors.
- `DV_ASCII=1 dv-tunnel`: use ASCII borders for limited encodings.
- `DV_NO_CLEAR=1 dv-tunnel`: preserve the screen during interactive redraws.
- Piped output and dumb terminals automatically disable colors; non-interactive
  output uses ASCII borders. There is no animation or background network polling.

The dashboard reads only local version metadata and whitelisted counters. It does
not display tokens, keys, endpoint URLs or private configuration content. Menu
rendering makes no transport, service, firewall or routing changes.
