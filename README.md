# aptv-playlists

Russian and movie channel playlists from [iptv-org](https://github.com/iptv-org/iptv), with `tvg-id` matched to the [iptvx.one](https://iptvx.one) EPG so players like APTV show a real TV guide.

Плейлисты iptv-org (русские каналы и кино) с привязкой к телепрограмме iptvx.one.

- Playlist (Russian): https://maxviazov.github.io/aptv-playlists/rus.m3u
- Playlist (movies): https://maxviazov.github.io/aptv-playlists/movies.m3u
- Кино (Russian movie/series channels + English-language movie channels): https://maxviazov.github.io/aptv-playlists/kino.m3u
- Хоккей (hockey channels: FloHockey, NHL; then regional channels that carry local KHL clubs; then general sports): https://maxviazov.github.io/aptv-playlists/hockey.m3u
- Юмор (Russian comedy channels + English-language comedy): https://maxviazov.github.io/aptv-playlists/humor.m3u
- EPG: https://maxviazov.github.io/aptv-playlists/epg.xml.gz

The themed playlists (kino, hockey, humor) are assembled by `build.py` from iptv-org (languages/rus, categories movies/sports/comedy), [smolnp/IPTVru](https://github.com/smolnp/IPTVru) , [Free-TV/IPTV](https://github.com/Free-TV/IPTV) and free FAST services (Pluto TV, Tubi via [BuddyChewChew/app-m3u-generator](https://github.com/BuddyChewChew/app-m3u-generator)), and contain only streams that passed the last check from Israel.

All playlists already point to the EPG in their header (`url-tvg`). The files are rebuilt daily by a GitHub Action (`build.py`, Python stdlib only). The EPG covers yesterday through the next 7 days and only includes matched channels.

Streams and schedules belong to their original sources; this repo just links them.

## Dead streams

`exclude.txt` lists stream URLs that `build.py` drops from both playlists. It is maintained by `tools/streamcheck.py`, which runs weekly on a Mac in Israel (stream availability depends on the viewer's location, so it can't run in GitHub Actions): a URL is excluded after failing two weekly checks in a row (timeouts, DNS/connection errors, 404/410, 5xx, non-stream links) and comes back as soon as it passes once. Geo-blocked answers (401/403/451) are never excluded from rus/movies; they are listed in `geoblocked.txt`, which only the themed playlists drop.
