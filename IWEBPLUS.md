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

`web/src/routes/report.py` is the single playback-report route source.
`iwebplus/` contains the private launcher, PyInstaller build inputs and offline regressions. The release workflow
builds `qqmusic-linux-x64-<version>-<sha256 prefix>` on Ubuntu 24.04 (glibc 2.39)
and publishes the executable, `qq-module.json`, the complete corresponding
source archive and the licenses archive. Upstream PyPI and Pages jobs are guarded to run only in the upstream repository;
this fork never publishes a PyPI package or writes documentation back to `main`.

Release builds require a clean committed tree, Ubuntu system Python 3.12, and
locked dependencies. The source archive contains that commit, Python dependency
source distributions, build inputs/tests and notices; exact Ubuntu system-library
source package versions are recorded in `system-libraries.json`. Normal system
libraries and the interpreter remain available from the Ubuntu source archive.
The archive also includes `fork.bundle`: clone it into a fresh directory to
restore the exact committed tree without GitHub, then install the locked build
dependencies and run `uv run --frozen python iwebplus/build.py` on Ubuntu 24.04.

Update the module version in `iwebplus/build.json`, `iwebplus/launcher.py` and
its notice, commit the changes, then push a new `v0.8.2-iwebplus.N` tag. Never
move a published tag. CI runs offline regressions and real standalone lifecycle,
authentication and OpenAPI acceptance tests before publishing the four assets.
The SDK/OpenAPI version remains `0.8.2`; it is separate from the module version.

The plugin downloads only assets from this fork's GitHub releases, verifies the
release asset digest and the manifest SHA-256/size, and never accepts a URL or
CLI argument from browser input.
