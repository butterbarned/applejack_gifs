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

## 9. The page on GitHub Pages (optional)

GitHub Pages serves only static files, so it can host the **page** while your Oracle server does the **drawing**:

```
 visitor ──▶ https://YOU.github.io/asset-tools/   (the page: GitHub Pages)
                │  the page's JavaScript fetch()es
                ▼
          https://api.your-domain.com             (Caddy ▶ asset_gui.py --public: your Oracle server)
```

The page already does this: `site/build.py` builds it with your server's address written into `config.js`, and every
request goes to `API_BASE + path`. Two things must be true on the server side, and **a bare IP address is not enough**:

* **The server needs an HTTPS name.** GitHub Pages is https, and browsers refuse to let an https page call a plain
  `http://1.2.3.4`. So give the server a name (a subdomain of your domain, e.g. `api.your-domain.com`, or a free
  one such as `yourname.duckdns.org`), point it at the server's IP, and let Caddy get its certificate (steps 2.4 and 2.6,
  using that name).
* **The server must say your page may call it (CORS).** Without that the browser blocks the answers. The page's
  *origin* is just the scheme and host, **no path and no trailing slash**: for `https://YOU.github.io/asset-tools/` it is
  `https://YOU.github.io` (with a custom domain it is that domain). Tell the installer:
  ```bash
  sudo CORS_ORIGIN=https://YOU.github.io /srv/asset-tools/app/deploy/install.sh api.your-domain.com https://github.com/YOU/asset-tools.git
  ```
  (running it again just rewrites the service file). Only that origin may use the API from a browser; browser requests from
  any other website get a 403.

Then on GitHub:

1. Repository **Settings → Pages → Build and deployment → Source: "GitHub Actions"**.
2. **Settings → Secrets and variables → Actions → Variables → New repository variable**: name `API_BASE`, value
   `https://api.your-domain.com` (the server's address, **https**, no path).
3. Push to `main` (or run the *pages* workflow from the Actions tab). A minute later the site is at
   `https://YOU.github.io/asset-tools/`.

From then on one `git push` updates both halves: the *pages* workflow republishes the page, and the server's timer pulls the
new backend (step 5). To point the page at a different server, change the `API_BASE` variable and run the workflow again.

Notes:
* The server's own address still serves the same page directly, as a fallback.
* A GitHub Pages site is public even if the repository is private (on normal accounts). The `basic_auth` option in step 7 does
  **not** work with a page hosted on Pages (browsers cannot ask for the password on those cross-site requests), so for a
  private site use the server's own page instead.
* To try it on your own computer first: `python site/build.py --api http://127.0.0.1:8793 --allow-http --out dist`, serve `dist`
  (`python -m http.server 9101 -d dist`) and start the server with `--public --port 8793 --cors-origin http://127.0.0.1:9101`.

## 10. Changing the domain

1. **DNS**: at your domain provider, point the new name at the server (an `A` record to the server's public IP). Check with
   `dig +short your.new.domain` (or `nslookup`) before going on.
2. **Caddy**: put the new name in `/etc/caddy/Caddyfile` instead of the old one, then
   ```bash
   sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
   ```
   Caddy gets a certificate for the new name by itself (ports 80 and 443 must be open, step 2).
3. **The page's service**: it only answers its own site name (`--allowed-host`), so change that too:
   ```bash
   sudo /srv/asset-tools/app/deploy/install.sh your.new.domain https://github.com/YOU/asset-tools.git
   ```
   (that rewrites the service file and restarts the page), or by hand:
   `sudo sed -i 's/--allowed-host OLD.NAME/--allowed-host NEW.NAME/' /etc/systemd/system/asset-gui.service && sudo systemctl daemon-reload && sudo systemctl restart asset-gui`.
4. **If the page is on GitHub Pages**, two more things: the server must allow the page's new address
   (`CORS_ORIGIN=https://page.address sudo -E ... install.sh`, step 9) and the `API_BASE` repository variable must hold the
   new API address; then run the *pages* workflow again.
5. Check: `curl -fsS https://your.new.domain/healthz` prints `ok`, and the page opens.

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
| page on GitHub Pages says "Could not reach the server" | open `https://api.your-domain.com/healthz` in a browser (must say ok: DNS and certificate); then the CORS origin: `systemctl cat asset-gui | grep cors-origin` must show exactly the page's origin (`https://YOU.github.io`, no path, no slash); blocked origins are logged as 403 in `journalctl -u asset-gui` |
| the *pages* workflow fails with "Set the repository variable API_BASE" | step 2 of section 9 |
| updates do not arrive | `systemctl list-timers asset-gui-update.timer`, `journalctl -u asset-gui-update`; private repo needs the deploy key |
