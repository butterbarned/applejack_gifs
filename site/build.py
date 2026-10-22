#!/usr/bin/env python3
"""Builds the website for GitHub Pages: the page, with the address of your server written in.

  python site/build.py --api https://api.your-domain.com --out dist

Makes dist/index.html (the very same page the server would serve), dist/config.js (the server address) and
dist/.nojekyll. The page then loads from GitHub Pages and talks to your server over HTTPS; the server must list
the Pages address with --cors-origin (deploy/DEPLOY.md, "The page on GitHub Pages").
"""
import argparse, json, os, sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import asset_gui  # the page lives in asset_gui.PAGE, so there is one copy of it to keep up to date


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", required=True, help="address of the server, e.g. https://api.your-domain.com (no path)")
    ap.add_argument("--out", default="dist", help="folder to write (default dist)")
    ap.add_argument("--allow-http", action="store_true", help="accept an http:// address (testing on your own computer only: browsers block it from an https page)")
    a = ap.parse_args()

    u = urlparse(a.api.strip())
    if u.scheme not in ("https", "http") or not u.netloc or u.path not in ("", "/") or u.query or u.fragment:
        sys.exit("--api must look like https://api.your-domain.com (an address with no path)")
    if u.scheme == "http" and not a.allow_http:
        sys.exit("--api must be https://...: a page on GitHub Pages is https, and browsers refuse to let it call a plain http address.\n"
                 "(a bare IP address cannot have a normal certificate: use a name, see deploy/DEPLOY.md)")
    api = "%s://%s" % (u.scheme, u.netloc)

    csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob:; connect-src %s; base-uri 'none'; form-action 'none'" % api)
    html = asset_gui.PAGE.replace("__TOKEN__", "")
    marker = "<title>Asset tools</title>"
    if marker not in html:
        sys.exit("the page has changed: build.py does not know where to put the settings")
    html = html.replace(marker, marker + '\n<meta http-equiv="Content-Security-Policy" content="%s">\n<script src="config.js"></script>' % csp, 1)

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(a.out, "config.js"), "w", encoding="utf-8") as f:
        f.write("window.API_BASE = %s;\n" % json.dumps(api))
    open(os.path.join(a.out, ".nojekyll"), "w").close()
    print("wrote %s/index.html and config.js for the server at %s" % (a.out, api))


if __name__ == "__main__":
    main()
