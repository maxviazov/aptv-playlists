#!/usr/bin/env python3
"""Rebuild iptv-org playlists with tvg-id matched to the iptvx.one EPG.

Usage: python3 build.py [--out DIR] [--base-url URL] [--epg-file PATH] [--exclude FILE]
Stdlib only.
"""
import argparse, datetime as dt, gzip, io, json, os, re, sys, urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from xml.sax.saxutils import escape, quoteattr

# Source playlists (all openly published, free streams only).
SOURCES = {
    "rus": "https://iptv-org.github.io/iptv/languages/rus.m3u",
    "movies": "https://iptv-org.github.io/iptv/categories/movies.m3u",
    "sports": "https://iptv-org.github.io/iptv/categories/sports.m3u",
    "comedy": "https://iptv-org.github.io/iptv/categories/comedy.m3u",
    "smolnp": "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVru.m3u",
    "freetv": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8",
}
PLAYLISTS = SOURCES  # backwards compatible name
# Themed playlists keep only streams that passed the check from Israel
# (exclude.txt and geoblocked.txt both apply); rus/movies drop only exclude.txt.
OUTPUTS = ("rus", "movies", "kino", "hockey", "humor")
STRICT = {"kino", "hockey", "humor"}
MIN_CHANNELS = {"rus": 50, "movies": 50}
EN_COUNTRIES = {"US", "UK", "GB", "CA", "AU", "IE", "NZ"}
HOCKEY_RE = re.compile(r"hockey|khl|nhl|хокке|кхл", re.I)
KHL_REGIONAL_RE = re.compile(r"belarus[- ]?5|беларусь[- ]?5|qazsport|казспорт|\bbst\b|\bбст\b|\btnv\b|\bтнв\b|"
                             r"nizhniy ?novgorod ?24|нижний новгород 24|санкт-петербург|sankt-peterburg", re.I)
SPORT_NAME_RE = re.compile(r"матч|match!|\bstart\b|старт|viju\+ ?sport|setanta|eurosport|okko|бокс|boks|"
                           r"mma|fight|extreme|экстрим|sport|спорт", re.I)
HUMOR_RE = re.compile(r"comedy|komed|комед|юмор|yumor|humor|\btnt|\bтнт|peretz|перец|\bche!|^че\b|че!|2x2|"
                      r"\bkvn|квн|sts love|стс love|gags|смех|laugh|fail ?army|ridiculous|funny|haha", re.I)
KINO_FREETV_RE = re.compile(r"смотрим: (мелодрамы|тайны|честный детектив)|кино|kino", re.I)
NON_EN_RE = re.compile(r"latam|latin|\bbr\b|brasil|brazil|espanol|colecao|coleccion|francais|italy|poland|vaya", re.I)
BADGE_RE = re.compile(r"\s*[\u24b6-\u24e9]+")
EPG_URL = "https://iptvx.one/EPG_LITE"
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
DAYS_BACK, DAYS_FWD = 1, 7

# Countries where the iptvx (Russian-language) schedule is a sensible match.
CIS = {"RU", "UA", "BY", "KZ", "KG", "UZ", "TJ", "TM", "AM", "AZ", "GE", "MD", "LV", "LT", "EE"}

# Manual decisions for cases the automatic matcher can't settle (key: tvg-id, or base id).
OVERRIDES = {
    "1HDMusicTelevision.ru": "1hd",
    "Moyastikhiya.ru": "morskoy",
    "Nano.ru@HD": "nano-hd",
    "Nano.ru@SD": "nano-tv",
    "Sport.kg": "utrk-sport-kg",
    "STV.by": "stv-by",
    "ViasatKino.ua@SD": "tv1000-cee",
    "ViasatKinoAction.ua@SD": "tv1000-action-cee",
    "vijuPlusPremiere.ru": "vip-premiere",
    "Channel12.ru": "12-omsk",
    "Terra.ua": None,
    "RTRPlaneta.ru@Europe": "rtr-planeta-eu",
    "RTRPlaneta.ru@Asia": "rtr-planeta-azia",
    "DW.de@Russian": "deutsche-welle-ru",
    "EuronewsRussian.fr": "euronews",
    "NationalGeographicWild.ru": "nat-geo-wild",
    "ShansonTV.ru": "shanson-tv",
    "ChannelOneCIS.ru": "pervy-kanal-csi",
    "OTR.ru": "otr",
    "NaxcivanTV.az": None,
    "JibekJoly.kz": None,
    "Nauka.ua": None,
    "Action24.gr": None,
}

QUALITY_TAIL = {"hd", "sd", "fhd", "uhd", "4k", "hq", "tv", "тв"}
TAG_RE = re.compile(r"\[[A-Za-z]{2,3}\]|\([^)]*\)")
CC_TAG_RE = re.compile(r"\[([A-Za-z]{2,3})\]")
# Cyrillic letters that look like Latin ones (CTC vs СТС, OTP vs ОТР)
HOMO = str.maketrans("авекмнорстух", "abekmhopctyx")


def norm(s, keep_tags=False):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\[(not 24/7|geo-blocked)\]", " ", s)
    s = re.sub(r"\(\d{3,4}[pi]\)", " ", s)
    if not keep_tags:
        s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = s.replace("&", " and ").replace("+", " plus ")
    s = re.sub(r"\.com\b", " ", s)
    s = s.translate(HOMO)
    toks = re.sub(r"[^\w]+", " ", s).split()
    full = "".join(toks)
    while len(toks) > 1 and toks[-1] in QUALITY_TAIL:
        toks.pop()
    out = "".join(toks)
    return out if len(out) >= 3 else full


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "aptv-playlists/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def load_exclude(path):
    """One URL per line; blank lines and '#' comments (whole-line or after whitespace) ignored."""
    urls = set()
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                urls.add(line.split()[0])
    return urls


def parse_time(s):
    s = s.strip()
    t = dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    m = re.search(r"([+-])(\d{2})(\d{2})", s[14:])
    off = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))) if m else dt.timedelta()
    if m and m.group(1) == "-":
        off = -off
    return t - off  # naive UTC


class Epg:
    def __init__(self, data):
        self.data = data
        self.names = {}
        self.strong = defaultdict(set)
        self.weak = defaultdict(set)  # tags removed; only untagged display-names
        for ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
            if el.tag == "channel":
                cid = el.get("id")
                names = [d.text or "" for d in el.findall("display-name")]
                self.names[cid] = names
                for n in names:
                    k = norm(n, keep_tags=True)
                    if len(k) >= 2:
                        self.strong[k].add(cid)
                    if not CC_TAG_RE.search(n):
                        k = norm(n)
                        if len(k) >= 2:
                            self.weak[k].add(cid)
                el.clear()
            elif el.tag == "programme":
                el.clear()

    def lookup(self, name, country=""):
        def ok(cid):
            tags = {t.upper() for n in self.names[cid] for t in CC_TAG_RE.findall(n)}
            return not tags or country in tags
        for index in (self.strong, self.weak):
            for k in (norm(name, keep_tags=True), norm(name)):
                if len(k) >= 3 and k in index:
                    ids = {i for i in index[k] if ok(i)}
                    if ids:
                        return ids
        return set()


def disambiguate(ids, epg, feed, name):
    ids = set(ids)
    if len(ids) == 1:
        return next(iter(ids))
    low = name.lower()
    m = re.match(r"plus(\d+)", feed.lower())
    m2 = re.search(r"\+(\d+)", name)
    shift = (m or m2).group(1) if (m or m2) else None
    plus_ids = {i for i in ids if re.search(r"-(pl|mn)\d+$", i)}
    if shift:
        hit = [i for i in plus_ids if i.endswith("-pl" + shift)]
        return hit[0] if len(hit) == 1 else None
    ids -= plus_ids

    def is_int(i):
        return i.endswith("-int") or "-int-" in i or any(
            re.search(r"international|\bint\b|международн", n.lower()) for n in epg.names[i])

    if "international" in low or feed.lower() in ("international", "europe", "europa"):
        ints = [i for i in ids if is_int(i)]
        if len(ints) > 1:
            hint = {"belarus": "by", "europe": "eu", "europa": "eu", "kazakhstan": "kz"}
            for word, suf in hint.items():
                if word in low or word == feed.lower():
                    ints = [i for i in ints if i.endswith(suf)] or ints
            if len(ints) > 1:
                ints = [i for i in ints if i.endswith("-int")] or ints
        if len(ints) == 1:
            return ints[0]
        return None
    ids = {i for i in ids if not is_int(i)} or ids
    # drop foreign/regional-tagged variants
    untagged = {i for i in ids if not all(TAG_RE.search(n) for n in epg.names[i])}
    ids = untagged or ids
    if "russia" in feed.lower():
        ru = [i for i in ids if i.endswith("-ru")]
        if len(ru) == 1:
            return ru[0]
    if len(ids) == 1:
        return next(iter(ids))
    # shortest id that prefixes all others
    base = min(ids, key=len)
    if all(i.startswith(base) for i in ids):
        return base
    hd = " hd" in " " + low.replace("(", " ") or feed.upper() == "HD"
    if not hd:
        nohd = {i for i in ids if not i.endswith("hd")}
        if len(nohd) == 1:
            return next(iter(nohd))
    return None


def match_entry(tid, name, chans, epg):
    base, _, feed = tid.partition("@")
    country = base.rsplit(".", 1)[-1].upper() if "." in base else ""
    for key in (tid, base):
        if key in OVERRIDES:
            return OVERRIDES[key], "override"
    c = chans.get(base)
    cands = [name] + ([c["name"]] + c.get("alt_names", []) if c else [])
    if country and country not in CIS and "russia" not in feed.lower() and "russia" not in name.lower():
        # only accept foreign channels through a Cyrillic alias
        cands = [n for n in cands if re.search(r"[а-яА-Я]", n)]
    low = name.lower()
    m = re.match(r"plus(\d+)", feed.lower()) or re.search(r"\+(\d+)", name)
    extra = []
    if m:
        extra += [n + v % m.group(1) for n in cands for v in (" (+%s)", " +%s")]
    if "international" in low or feed.lower() in ("international", "europe", "europa"):
        extra += [n + " International" for n in cands if "international" not in n.lower()]
    def resolve(names):
        res = set()
        for n in names:
            ids = epg.lookup(n, country)
            if ids:
                r = disambiguate(ids, epg, feed, name)
                if r:
                    res.add(r)
        return res

    cyr = lambda lst: [n for n in lst if re.search(r"[а-яА-ЯёЁ]", n)]
    results = set()
    for group in ((cyr(extra), extra) if m or extra else ()):
        results = resolve(group)
        if len(results) == 1:
            return results.pop(), "auto"
    if m:
        return None, "no-timeshift-feed"
    for group in (cyr(cands), cands):
        results = resolve(group)
        if len(results) == 1:
            return results.pop(), "auto"
    return None, ("conflict:" + ",".join(sorted(results))) if results else "none"


def parse_playlist(text):
    """-> list of entries {extinf, opts, url, name, tid, group, country}"""
    entries, block = [], None
    for line in text.splitlines():
        if line.startswith(("#EXTM3U", "#PLAYLIST")):
            continue
        if line.startswith("#EXTINF"):
            line = BADGE_RE.sub("", line).rstrip()
            block = {"extinf": line, "opts": []}
        elif block is not None and line.startswith("#"):
            if line.strip():
                block["opts"].append(line.strip())
        elif block is not None and line.strip():
            line_ = block["extinf"]
            m = re.search(r'tvg-id="([^"]*)"', line_)
            g = re.search(r'group-title="([^"]*)"', line_)
            block.update(url=line.strip(), name=line_.rsplit(",", 1)[1].strip() if "," in line_ else "",
                         tid=m.group(1) if m else "", group=g.group(1) if g else "")
            base = block["tid"].partition("@")[0]
            block["country"] = base.rsplit(".", 1)[-1].upper() if "." in base else ""
            for o in block["opts"]:
                if o.startswith("#EXTVLCOPT:http-user-agent="):
                    block["ua"] = o.split("=", 1)[1]
                elif o.startswith("#EXTVLCOPT:http-referrer="):
                    block["ref"] = o.split("=", 1)[1]
            entries.append(block)
            block = None
    return entries


def load_sources():
    return {k: parse_playlist(fetch(u).decode("utf-8", "replace")) for k, u in SOURCES.items()}


def select(src):
    """-> {output: [(entry, group-title override or None)]} before any exclusion, de-duplicated by URL."""
    groups = lambda e: {p.strip() for p in e["group"].split(";")}
    ru_like = lambda e: e["country"] in CIS or re.search(r"[а-яА-ЯёЁ]", e["name"])
    cands = {"rus": [(e, None) for e in src["rus"]], "movies": [(e, None) for e in src["movies"]],
             "kino": [], "hockey": [], "humor": []}
    russian = src["rus"] + src["smolnp"] + [e for e in src["freetv"] if e["group"] == "Russia"]
    k = cands["kino"]
    k += [(e, "Кино и сериалы") for e in src["rus"] if groups(e) & {"Movies", "Series", "Classic"}]
    k += [(e, "Кино и сериалы") for e in src["smolnp"] if e["group"].startswith("Кино")]
    k += [(e, "Кино и сериалы") for e in src["freetv"] if e["group"] == "Russia" and KINO_FREETV_RE.search(e["name"])]
    k += [(e, "International") for e in src["movies"] if e["country"] in EN_COUNTRIES]
    h = cands["hockey"]
    allsrc = [e for key in ("rus", "sports", "smolnp", "freetv") for e in src[key]]
    h += [(e, "Хоккей") for e in allsrc if HOCKEY_RE.search(e["name"]) or "khl" in e["url"].lower()]
    h += [(e, "Региональные (КХЛ)") for e in russian if KHL_REGIONAL_RE.search(e["name"])]
    h += [(e, "Спорт") for e in src["rus"] if "Sports" in groups(e)]
    h += [(e, "Спорт") for e in src["smolnp"] if e["group"] == "Спортивные"]
    h += [(e, "Спорт") for e in src["freetv"] if e["group"] == "Russia" and SPORT_NAME_RE.search(e["name"])]
    u = cands["humor"]
    u += [(e, "Юмор") for e in russian if "Comedy" in groups(e) or HUMOR_RE.search(e["name"])]
    u += [(e, "Юмор") for e in src["comedy"] if ru_like(e)]
    u += [(e, "Comedy (EN)") for e in src["comedy"] if e["country"] in EN_COUNTRIES and not NON_EN_RE.search(e["name"])]
    out = {}
    for key, items in cands.items():
        seen, lst = set(), []
        for e, g in items:
            if e["url"] not in seen:
                seen.add(e["url"])
                lst.append((e, g))
        out[key] = lst
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--epg-file", help="use a local (optionally gzipped) EPG instead of downloading")
    ap.add_argument("--report", help="write match report JSON here")
    ap.add_argument("--exclude", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "exclude.txt"),
                    help="file with stream URLs to drop (one per line, # comments)")
    ap.add_argument("--geoblocked", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "geoblocked.txt"),
                    help="URLs blocked from Israel (401/403/451): dropped from the themed playlists only")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    print("downloading EPG...", file=sys.stderr)
    if a.epg_file:
        data = open(a.epg_file, "rb").read()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
    else:
        data = fetch(EPG_URL)
    epg = Epg(data)
    if len(epg.names) < 1000:
        sys.exit("EPG looks broken (%d channels), not overwriting outputs" % len(epg.names))
    chans = {c["id"]: c for c in json.loads(fetch(CHANNELS_URL))}
    epg_url = (a.base_url.rstrip("/") + "/epg.xml.gz") if a.base_url else "epg.xml.gz"

    excluded = load_exclude(a.exclude)
    geo = load_exclude(a.geoblocked)
    print("exclude list: %d URLs, geoblocked: %d" % (len(excluded), len(geo)), file=sys.stderr)
    try:
        selected = select(load_sources())
    except Exception as ex:
        sys.exit("source download failed (%s), not overwriting outputs" % ex)
    cache = {}

    def epg_id(e):
        key = (e["tid"], e["name"])
        if key not in cache:
            if e["tid"] in epg.names:  # already an iptvx id (smolnp list)
                cache[key] = (e["tid"], "source")
            else:
                cache[key] = match_entry(e["tid"], e["name"], chans, epg)
        return cache[key]

    used, report, outputs = set(), {}, {}
    for key in OUTPUTS:
        drop = excluded | geo if key in STRICT else excluded
        out, rows, skipped = ['#EXTM3U url-tvg="%s" x-tvg-url="%s"' % (epg_url, epg_url)], [], 0
        for e, group in selected[key]:
            if e["url"] in drop:
                skipped += 1
                continue
            line = e["extinf"]
            cid, how = epg_id(e)
            if cid:
                used.add(cid)
                dn = epg.names[cid][0].replace('"', "'")
                attrs = 'tvg-id="%s" tvg-name="%s"' % (cid, dn)
                m = re.search(r'tvg-id="[^"]*"', line)
                if m:
                    line = line.replace(m.group(0), attrs, 1)
                else:
                    line = re.sub(r"^#EXTINF:(-?\d+)", r"#EXTINF:\1 " + attrs, line, 1)
                line = re.sub(r'(tvg-name="[^"]*")(.*?)\s+tvg-name="[^"]*"', r"\1\2", line)
            if group:
                if 'group-title="' in line:
                    line = re.sub(r'group-title="[^"]*"', 'group-title="%s"' % group, line, 1)
                else:
                    head, _, tail = line.rpartition(",")
                    line = '%s group-title="%s",%s' % (head.rstrip(), group, tail)
            out.extend([line] + e["opts"] + [e["url"]])
            rows.append({"tvg_id": e["tid"], "name": e["name"], "epg_id": cid, "how": how,
                         "epg_name": epg.names[cid][0] if cid else None})
        outputs[key + ".m3u"] = "\n".join(out) + "\n"
        n = sum(1 for r in rows if r["epg_id"])
        print("%s: %d channels (%d excluded), EPG matched %d" % (key, len(rows), skipped, n), file=sys.stderr)
        if len(rows) < MIN_CHANNELS.get(key, 3):
            sys.exit("%s looks broken (%d channels), not overwriting outputs" % (key, len(rows)))
        report[key] = rows
    for fn, text in outputs.items():
        with open(os.path.join(a.out, fn), "w", encoding="utf-8") as f:
            f.write(text)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    lo = dt.datetime(now.year, now.month, now.day) - dt.timedelta(days=DAYS_BACK)
    hi = lo + dt.timedelta(days=DAYS_BACK + DAYS_FWD + 1)
    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="utf-8"?>\n<tv generator-info-name="aptv-playlists (source: iptvx.one)">\n')
    progs = 0
    for ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if el.tag == "channel":
            if el.get("id") in used:
                buf.write(ET.tostring(el, encoding="unicode").strip() + "\n")
            el.clear()
        elif el.tag == "programme":
            if el.get("channel") in used:
                try:
                    st, sp = parse_time(el.get("start")), parse_time(el.get("stop") or el.get("start"))
                except Exception:
                    st = sp = None
                if st and sp >= lo and st < hi:
                    buf.write(ET.tostring(el, encoding="unicode").strip() + "\n")
                    progs += 1
            el.clear()
    buf.write("</tv>\n")
    with open(os.path.join(a.out, "epg.xml.gz"), "wb") as raw, \
            gzip.GzipFile(filename="epg.xml", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as f:
        f.write(buf.getvalue().encode("utf-8"))
    print("epg: %d channels, %d programmes" % (len(used), progs), file=sys.stderr)
    if a.report:
        json.dump(report, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
