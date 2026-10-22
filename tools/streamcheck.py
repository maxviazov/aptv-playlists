#!/usr/bin/env python3
"""Check every stream in the iptv-org source playlists and maintain exclude.txt.

Run it from a machine whose IP is where the playlists are watched (the checks are
location-dependent, so this is NOT run in GitHub Actions).

Rules:
  * a URL is added to exclude.txt after failing N consecutive runs (default 2, --seed uses 1);
  * a URL in exclude.txt that passes (or is merely geo-blocked) once is removed;
  * geo-blocked answers (401/403/451) go to geoblocked.txt (dropped from the themed playlists
    only); 429 and unsupported schemes (rtmp, udp...) are never listed.

Usage:
  python3 tools/streamcheck.py [--seed] [--git] [--state FILE] [--dry-run]

--git: git pull first; if exclude.txt changed, commit + push it and trigger the
`update` workflow (gh workflow run) so GitHub Pages rebuilds.
Stdlib + /usr/bin/curl only, Python 3.9+.
"""
import argparse, concurrent.futures as cf, datetime as dt, json, os, subprocess, sys, time
from urllib.parse import urljoin, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from build import OUTPUTS, load_sources, select  # noqa: E402

CURL = "/usr/bin/curl" if os.path.exists("/usr/bin/curl") else "curl"
GH = next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
EXCLUDE = os.path.join(REPO, "exclude.txt")
GEOBLOCKED = os.path.join(REPO, "geoblocked.txt")
DEFAULT_STATE = os.path.expanduser("~/Library/Application Support/aptv-streamcheck/state.json")
GEO_CODES = {401, 403, 451}  # alive but blocked for us: kept in rus/movies, dropped from themed lists
KEEP_CODES = {429}


def log(*a):
    print(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def parse_m3u(text, playlist):
    out, cur = [], None
    for l in text.splitlines():
        l = l.strip()
        if l.startswith("#EXTINF"):
            cur = {"name": l.rsplit(",", 1)[-1].strip(), "ua": None, "ref": None, "pl": playlist}
        elif l.startswith("#EXTVLCOPT:http-user-agent=") and cur:
            cur["ua"] = l.split("=", 1)[1]
        elif l.startswith("#EXTVLCOPT:http-referrer=") and cur:
            cur["ref"] = l.split("=", 1)[1]
        elif l and not l.startswith("#") and cur:
            cur["url"] = l
            out.append(cur)
            cur = None
    return out


def curl(url, e, timeout, rng=False):
    cmd = [CURL, "-sL", "--compressed", "-m", str(timeout), "--connect-timeout", str(min(timeout, 10)),
           "-A", e.get("ua") or UA, "-o", "-", "-w", "\n__CODE__%{http_code} %{url_effective}"]
    if e.get("ref"):
        cmd += ["-e", e["ref"]]
    if rng:
        cmd += ["-r", "0-32767"]
    p = subprocess.run(cmd + [url], capture_output=True)
    out = p.stdout
    i = out.rfind(b"\n__CODE__")
    if i < 0:
        return p.returncode, 0, url, out
    code, _, eff = out[i + 9:].decode("utf-8", "replace").partition(" ")
    return p.returncode, int(code or 0), eff or url, out[:i]


def err(rc, code):
    """-> (status, reason); status FAIL (dead), GEO (blocked for us) or KEEP (unknown, leave alone)"""
    if code in GEO_CODES:
        return "GEO", "HTTP %d" % code
    if code in KEEP_CODES:
        return "KEEP", "HTTP %d" % code
    if code in (404, 410) or code >= 500:
        return "FAIL", "HTTP %d" % code
    if 400 <= code < 500:
        return "KEEP", "HTTP %d" % code
    names = {6: "DNS", 7: "connection refused", 28: "timeout", 35: "TLS", 51: "TLS", 60: "TLS",
             52: "empty reply", 56: "connection reset", 47: "redirect loop"}
    if rc in names:
        return "FAIL", names[rc]
    return "KEEP", "curl rc=%d http=%d" % (rc, code)


def first_uri(text):
    for l in text.splitlines():
        l = l.strip()
        if l and not l.startswith("#"):
            return l


def check_once(e, timeout):
    """-> (status, reason) with status OK / FAIL (excludable) / KEEP (don't exclude)"""
    url = e["url"]
    if urlparse(url).scheme not in ("http", "https"):
        return "KEEP", "unchecked scheme"
    rc, code, eff, body = curl(url, e, timeout)
    if rc == 28 and 200 <= code < 300 and body:
        return "OK", "continuous stream"
    if not (rc == 0 and 200 <= code < 300):
        st, why = err(rc, code)
        return st, "playlist: " + why
    t = body[:200000].decode("utf-8", "replace")
    head = t[:3000].lstrip().lower()
    if "#extm3u" not in head:
        if head.startswith(("<!doctype", "<html")) or "<html" in head[:500]:
            return "FAIL", "web page, not a stream"
        if "<mpd" in head:
            return "OK", "DASH"
        return ("OK", "non-HLS stream") if len(body) > 1000 else ("FAIL", "empty/invalid playlist")
    for _ in range(3):
        if "#EXT-X-STREAM-INF" not in t:
            break
        v = first_uri(t)
        if not v:
            return "FAIL", "master playlist without variants"
        rc, code, eff, body = curl(urljoin(eff, v), e, timeout)
        if not (rc == 0 and 200 <= code < 300):
            st, why = err(rc, code)
            return st, "variant: " + why
        t = body[:200000].decode("utf-8", "replace")
    if "#EXTINF" not in t:
        return "FAIL", "no segments"
    seg = first_uri(t)
    rc, code, _, body = curl(urljoin(eff, seg), e, timeout, rng=True)
    if rc in (0, 28) and 200 <= code < 300 and body:
        return "OK", ""
    st, why = err(rc, code)
    return st, "segment: " + why


def check(e):
    st, why = check_once(e, 10)
    if st in ("FAIL", "GEO"):  # second chance with a longer timeout, to skip slow-but-alive servers
        time.sleep(2)
        st, why = check_once(e, 25)
    return st, why


def read_exclude(path=EXCLUDE):
    urls = set()
    if os.path.exists(path):
        for l in open(path, encoding="utf-8"):
            l = l.strip()
            if l and not l.startswith("#"):
                urls.add(l.split()[0])
    return urls


def write_list(path, rows, title):
    lines = [title,
             "# Maintained by tools/streamcheck.py (weekly check from Israel); one URL per line,",
             "# anything after whitespace + '#' is a comment. A URL comes back as soon as it passes once.",
             ""]
    for pl, name, url, why in sorted(rows):
        lines.append("%s  # [%s] %s: %s" % (url, pl, name.replace("#", ""), why))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def git(*args, check=True):
    env = dict(os.environ)
    # don't depend on an interactive agent (1Password etc.) when running under launchd
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o IdentityAgent=none -o IdentitiesOnly=yes "
                   "-i %s" % os.path.expanduser("~/.ssh/id_ed25519"))
    p = subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
    return p


def push():
    if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
        return "ssh"
    git("pull", "-q", "--rebase", "origin", "main")
    if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
        return "ssh"
    # fallback: HTTPS with the gh login
    git("-c", "credential.helper=", "-c", "credential.helper=!%s auth git-credential" % GH,
        "push", "-q", "https://github.com/maxviazov/aptv-playlists.git", "HEAD:main")
    return "https (gh)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="exclude after a single failure")
    ap.add_argument("--git", action="store_true", help="pull, then commit/push exclude.txt and run the workflow")
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--dry-run", action="store_true", help="don't write exclude.txt or state")
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()
    threshold = 1 if a.seed else 2
    log("start (threshold %d)" % threshold)
    if a.git:
        git("pull", "-q", "--rebase", "--autostash", "origin", "main")

    try:
        selected = select(load_sources())
    except Exception as ex:
        sys.exit("cannot download source playlists (%s), nothing changed" % ex)
    entries, seen = [], set()
    for key in OUTPUTS:
        if len(selected[key]) < 3:
            sys.exit("%s looks broken (%d entries), nothing changed" % (key, len(selected[key])))
        for e, _ in selected[key]:
            if e["url"] not in seen:
                seen.add(e["url"])
                entries.append(dict(e, pl=key))
    log("checking %d unique stream URLs" % len(entries))
    with cf.ThreadPoolExecutor(a.workers) as ex:
        results = list(ex.map(check, entries))

    counts = {}
    for st, _ in results:
        counts[st] = counts.get(st, 0) + 1
    log("results: " + ", ".join("%s %d" % kv for kv in sorted(counts.items())))
    if counts.get("OK", 0) < 0.3 * len(entries):
        sys.exit("only %d OK out of %d: network problem? nothing changed" % (counts.get("OK", 0), len(entries)))

    state = {}
    if os.path.exists(a.state):
        state = json.load(open(a.state, encoding="utf-8"))
    old = read_exclude(EXCLUDE) | read_exclude(GEOBLOCKED)
    today = dt.date.today().isoformat()
    new_state, rows, geo_rows = {}, [], []
    for e, (st, why) in zip(entries, results):
        prev = state.get(e["url"], {})
        if st == "KEEP" and why != "unchecked scheme":
            st = "GEO"  # 429 / other 4xx / odd curl errors: not playable here, keep out of themed lists
        if st in ("FAIL", "GEO"):
            s = {"fails": prev.get("fails", 0) + 1, "since": prev.get("since", today), "why": why}
            new_state[e["url"]] = s
            if s["fails"] >= threshold or e["url"] in old:
                row = (e["pl"], e["name"], e["url"], "%s (since %s)" % (why, s["since"]))
                (rows if st == "FAIL" else geo_rows).append(row)
    listed = {r[2] for r in rows + geo_rows}
    added, removed = listed - old, old - listed
    log("exclude: %d dead + %d geo-blocked URLs (+%d, -%d)" % (len(rows), len(geo_rows), len(added), len(removed)))
    if a.dry_run:
        return
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    json.dump(new_state, open(a.state, "w", encoding="utf-8"), indent=1)
    write_list(EXCLUDE, rows, "# Dead stream URLs, dropped from all published playlists by build.py.")
    write_list(GEOBLOCKED, geo_rows, "# Stream URLs that answer 401/403/451 (or other 4xx) from Israel: dropped from kino/hockey/humor only.")

    if a.git:
        if not git("status", "--porcelain", "exclude.txt", "geoblocked.txt").stdout.strip():
            log("exclude lists unchanged, nothing to push")
            return
        git("add", "exclude.txt", "geoblocked.txt")
        git("-c", "user.name=aptv-streamcheck", "-c", "user.email=maxviazov@users.noreply.github.com",
            "commit", "-q", "-m", "streamcheck %s: %d dead, %d geo-blocked (+%d, -%d)" % (today, len(rows), len(geo_rows), len(added), len(removed)))
        log("pushed via " + push())
        p = subprocess.run([GH, "workflow", "run", "update.yml", "-R", "maxviazov/aptv-playlists"],
                           capture_output=True, text=True)
        log("workflow run: " + ("ok" if p.returncode == 0 else "FAILED " + p.stderr.strip()))
    log("done")


if __name__ == "__main__":
    main()
