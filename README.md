# aptv-playlists

Russian and movie channel playlists from [iptv-org](https://github.com/iptv-org/iptv), with `tvg-id` matched to the [iptvx.one](https://iptvx.one) EPG so players like APTV show a real TV guide.

Плейлисты iptv-org (русские каналы и кино) с привязкой к телепрограмме iptvx.one.

- Playlist (Russian): https://maxviazov.github.io/aptv-playlists/rus.m3u
- Playlist (movies): https://maxviazov.github.io/aptv-playlists/movies.m3u
- EPG: https://maxviazov.github.io/aptv-playlists/epg.xml.gz

Both playlists already point to the EPG in their header (`url-tvg`). The files are rebuilt daily by a GitHub Action (`build.py`, Python stdlib only). The EPG covers yesterday through the next 7 days and only includes matched channels.

Streams and schedules belong to their original sources; this repo just links them.
