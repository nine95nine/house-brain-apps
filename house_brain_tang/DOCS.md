# House Brain Tang 0.1.0

Owner-approved design: `docs/house-brain/MACBOOK_TANG_UNLOCK_DESIGN_R1_2026-11-08.md` in
`nine95nine/home-assistant-whole-home` (initiative #229). `CONTROL_AUTHORITY=NONE`.

## What it runs

- **Base:** Home Assistant's Debian base image (Debian 13 "trixie"), pinned by digest.
- **Packages (exact versions):** `tang 15-2`, `socat 1.8.0.3-1+deb13u1`, `python3 3.13.5-1` (for `/health`
  only), all from Debian.
- **Server:** `socat TCP-LISTEN:7500,reuseaddr,fork EXEC:"/usr/libexec/tangd /data/tang"`. Each connection
  is answered by Debian's `tangd`; nothing of ours is on the request path.
- **User:** the App starts as root only to create `/data/tang` and give it to Debian's `_tang` user, then
  drops to `_tang` for good (no supplementary groups) before anything else runs.

## Keys

- Made once, on first start, by Tang's own `tangd-keygen` into `/data/tang` (directory mode 700). The App
  runs the key tool only when no active key exists, and never rotates, rewrites or deletes a key.
- Excluded from Home Assistant backups (`backup_exclude` in `config.yaml`), so a leaked backup cannot help
  unlock the disk.
- Lost keys (Pi or data lost): the MacBook falls back to its passphrase; Clevis is then re-bound to the new
  keys. Rotation is an explicit owner step, never automatic.

## Network

- `7500/tcp` is published on the Pi for the home network. **Never forward it on the router.** Tang has no
  login and needs none: what it serves is useless without the MacBook's own disk.
- `/health` (`house_brain_app_health.v1`) is served on `8099` to the internal Supervisor network and loopback
  only; `8099` is not published. It is OK only when Tang itself answered a valid advertisement at that
  moment; otherwise FAILED with `TANG_ADVERTISEMENT_UNREACHABLE` or `TANG_ADVERTISEMENT_INVALID`. It never
  carries a key, a thumbprint or the advertisement body.

## Log

On start the log shows whether keys were created or already present (a count only), and the signing-key
thumbprint (SHA-256) from Debian's `tang-show-keys`. Compare that thumbprint with what the MacBook sees at
`http://<pi>:7500/adv` before binding Clevis (design §5 step 3). No key material is ever logged.

## What it does not do

No Home Assistant API, no Supervisor API, no entity, no service call, no proposal, no physical action.
Installing it does not change the MacBook: binding Clevis is a separate owner step on the MacBook.
