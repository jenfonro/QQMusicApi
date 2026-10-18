# iWebPlus controlled fork

This repository is a fork of [L-1124/QQMusicApi](https://github.com/L-1124/QQMusicApi)
maintained by the iWebPlus/Songloft plugin. It is **not** an upstream-official
release channel and its binaries are **not** upstream-official binaries.

`main` carries three changes required by the plugin:

1. response-cookie snapshot reads `CookieJar.items()` so same-name cookies from
   different domains do not raise `CookieConflictError` during QQ QR login;
2. the radar recommendation route is bound to the caller credential and is not
   placed in the public cache or request coalescer;
3. a fixed `/report/play_duration` route forwards one verified playback snapshot
   for the authenticated account.

`iwebplus/` contains the private launcher, the playback-report route source,
PyInstaller/material build inputs and offline regressions. The release workflow
builds `qqmusic-linux-x64-<version>-<sha256 prefix>` on Ubuntu 24.04 (glibc 2.39)
and publishes the executable, `qq-module.json`, the complete corresponding
source archive and the licenses archive. Upstream `release.yml` is disabled on
this fork so no PyPI package is published.

The plugin downloads only assets from this fork's GitHub releases, verifies the
release asset digest and the manifest SHA-256/size, and never accepts a URL or
CLI argument from browser input.
