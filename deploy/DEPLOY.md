# Putting the GIF maker on an Oracle VPS

What you get: `https://gif.your-domain.com` serves the **GIF maker only** (public mode). New code you push
to GitHub reaches the server within 5 minutes, is checked, and is rolled back automatically if it breaks.

```
 visitor ──https──▶ Caddy (80/443, certificate) ──▶ asset_gui.py --public (127.0.0.1:8765)
                                                      reads /srv/asset-tools/cache (read-only)
 you ──git push──▶ GitHub ◀──pull every 5 min── update.sh (checks, restarts, rolls back)
```

## 0. What public mode is, and is not

The page on your own computer can run the other scripts, change the cache folder and download from the game
CDN. None of that may be reachable from the internet, so `--public` removes it:

| | private (your computer) | `--public` (the server) |
|---|---|---|
| tabs for mirror_assets, composite_avatar... | yes | **no** |
| change cache folder, run programs | yes | **no** |
| saved colours / outfits | a file on the computer | **in each visitor's browser** |
| downloads missing files from the game CDN | yes | **no** (turn on with `--allow-download` if you really want it) |
| who may use it | whoever has the secret link | anyone, but: one site name only, 40 previews/exports per visitor per minute, 2 drawn at once, size capped at 3x |

It listens on `127.0.0.1` only; Caddy is the only thing that talks to it. The app runs as its own user with a
read-only view of the files.

**Be aware:** the pictures it draws are Animal Jam's artwork. Sharing them publicly may go against the game's terms,
so you may prefer to keep the site private (see "Keep it private" below). That is your decision; this is not legal advice.

## 1. Put the code on GitHub (once)

Use a **new, separate repository** for this folder (`asset-tools`), not the Burrow server repo. This folder
has no game files, no cache and no decompiled code in it (`.gitignore` keeps it that way).

```bash
cd ~/Documents/asset-tools
git init -b main
git add .
git status          # CHECK: only .py, .md, deploy/, tests/, .github/, .gitignore. No cache, no *-out folders, no .zip
git commit -m "Asset tools"
# make an EMPTY repository on github.com first (no README), then:
git remote add origin https://github.com/YOU/asset-tools.git
git push -u origin main
```

From then on the server follows `main`: edit, commit, push. (`.github/workflows/ci.yml` runs a few checks on every push.)

## 2. The Oracle server

1. **Create an instance**: Ubuntu 22.04 or 24.04. Either Always Free shape is enough: the page uses about 180 MB
   while drawing (the 1 GB AMD micro works; an Ampere ARM instance is roomier). Give the boot volume at least 30 GB
   if you will host the clothing files (about 4 GB, but a million small files wastes disk blocks).
   Save the SSH key it makes. Reserve a public IP so it does not change.
   Oracle's Always Free terms allow reclaiming instances that sit idle for a long time; check the current policy.
2. **Open the ports** in the cloud: *Networking → Virtual Cloud Networks → your VCN → Security Lists →
   Add Ingress Rules*: source `0.0.0.0/0`, TCP, destination port `80`, and another for `443`.
3. **Open them on the machine too** (Oracle's Ubuntu images block everything but SSH with iptables):
   ```bash
   sudo iptables -L INPUT --line-numbers      # note the line number of the final REJECT rule
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save             # (apt install iptables-persistent if it is missing)
   ```
   The new rules must sit *above* the REJECT rule; adjust the `6` if yours is on another line.
4. **DNS**: at your domain provider add an `A` record `gif.your-domain.com → the server's public IP`.
5. **Install the page** (this repo must already be on GitHub):
   ```bash
   ssh ubuntu@SERVER
   sudo apt-get update && sudo apt-get install -y git
   git clone https://github.com/YOU/asset-tools.git /tmp/asset-tools    # only to get install.sh
   sudo /tmp/asset-tools/deploy/install.sh gif.your-domain.com https://github.com/YOU/asset-tools.git
   ```
   It creates the `assettools` user, clones the code to `/srv/asset-tools/app`, makes a Python environment with
   Pillow, installs the service and the update timer, and starts them.
   *Private repository?* Make a read-only deploy key: `sudo -u assettools mkdir -p /srv/asset-tools/.ssh &&
   sudo -u assettools ssh-keygen -t ed25519 -N "" -f /srv/asset-tools/.ssh/id_ed25519`, add the `.pub` file under the
   repo's *Settings → Deploy keys*, run `sudo -u assettools ssh -T git@github.com` once to accept GitHub's host key,
   and use `git@github.com:YOU/asset-tools.git` as the address.
6. **Caddy (HTTPS)**. Install it from Caddy's own instructions (<https://caddyserver.com/docs/install#debian-ubuntu-raspbian>),
   then:
   ```bash
   sudo cp /srv/asset-tools/app/deploy/Caddyfile.example /etc/caddy/Caddyfile
   sudo nano /etc/caddy/Caddyfile        # change gif.example.com to your domain
   sudo systemctl reload caddy
   ```
   Caddy fetches and renews the certificate by itself once the DNS record points here and ports 80/443 are open.

## 3. The game files

The page only reads `/srv/asset-tools/cache`. Fill it with the downloader, **as the `assettools` user**, in `tmux`
because the big ones take hours (it resumes if interrupted):

```bash
M="sudo -u assettools /srv/asset-tools/venv/bin/python /srv/asset-tools/app/mirror_assets.py --out /srv/asset-tools/cache"
$M --types defs,icons                       # names, palette, shop icons (quick)
$M --types avatars,clothing --avatar 8      # one animal with all its clothing (a good first test)
$M --types avatars,clothing                 # every animal (~1M files, ~4 GB)
```

The page looks at the folder as it works and refreshes its lists about once a minute, so new files appear without a restart.
Public mode never downloads for visitors, so it offers only what is in that folder.

## 4. Check it

```bash
curl -fsS http://127.0.0.1:8765/healthz               # ok
sudo systemctl status asset-gui                       # active (running)
journalctl -u asset-gui -n 30 --no-pager              # only trouble is logged
```
Then open `https://gif.your-domain.com`.

## 5. Updating the code

Push to GitHub. Within 5 minutes `asset-gui-update.timer` runs `update.sh`, which fetches, checks that every script
parses, restarts the page, waits for `/healthz`, and **rolls back to the previous version if anything fails**. A commit
that failed is remembered and not retried until a newer one arrives.

```bash
sudo /srv/asset-tools/app/deploy/update.sh        # update right now
journalctl -u asset-gui-update -n 30 --no-pager   # what the last updates did (look for ROLLING BACK)
```

* Never edit files on the server: the next update resets them (`git reset --hard`).
* If you change anything in `deploy/` (the service file, the timer), run `install.sh` again; `update.sh` only updates code.
* Want updates the moment you push instead of every 5 minutes? See `github-deploy.yml.example` (optional).

## 6. A new game build, or more files

* More avatars or clothing: run the downloader again (step 3). Nothing else to do.
* A new build number: change `--build 1830` in `deploy/asset-gui.service`, push, run `install.sh` once more, and download
  the new build's files (`--build NEW` on the downloader).

## 7. Keep it private

Uncomment the `basic_auth` block in `/etc/caddy/Caddyfile` (make the hash with `caddy hash-password`) and reload Caddy:
visitors then need your shared login. Good while you are deciding who the site is for.

## 8. Tuning and safety

* `--rate 40` previews/exports per visitor per minute, `--max-concurrent 2` drawn at once, `--max-scale 3`: edit them in
  `asset-gui.service`. If drawing makes the page feel slow for everyone, lower `--max-concurrent` or `--rate`.
* Visitors who are over the limit get "Too many requests"; when all drawing slots are busy for 25 seconds they get "server is busy".
* Keep the machine updated (`sudo apt install unattended-upgrades`), use SSH keys only, and consider putting Cloudflare in front if the site gets popular.
* If you change the Caddy setup, keep `--trust-proxy` only while Caddy (or another proxy you control) is the only way in: the rate limit trusts the address in `X-Forwarded-For`.

## Troubleshooting

| you see | check |
|---|---|
| browser can't connect | DNS record, step 2 ports (**both** the cloud list and iptables), `sudo systemctl status caddy` |
| certificate error | the domain must resolve to this server before Caddy can get a certificate: `journalctl -u caddy -n 30` |
| 502 from Caddy | the page is not running: `systemctl status asset-gui`, `journalctl -u asset-gui` |
| 403 forbidden | the site name in `--allowed-host` (in the service file) must be the one in the address bar |
| "Could not load the game data" | the cache is empty or in the wrong place: step 3, and `--cache` in the service file |
| an avatar is missing from the list | its files are not in the cache: download it (step 3) |
| an item is missing from a picture ("not downloaded here") | the clothing files for that avatar are not downloaded |
| updates do not arrive | `systemctl list-timers asset-gui-update.timer`, `journalctl -u asset-gui-update`; private repo needs the deploy key |
