# House Brain Tang

A Tang key server on the Pi, so the MacBook's encrypted disk can unlock itself at boot when it is at home
(Clevis + Tang, network-bound disk encryption). Away from home, or with the Pi down, the MacBook waits for
its passphrase as before: the passphrase slot always stays.

It runs Debian's own signed `tang` package behind Debian's `socat`, as a non-root user. No code of ours
does any cryptography. Keys are made once by Tang's own tool, live in the App's data folder (mode 700) and
are left out of Home Assistant backups. No Home Assistant or Supervisor access, no entity, no proposal.

See [DOCS.md](DOCS.md).
