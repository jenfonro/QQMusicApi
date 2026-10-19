# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the standalone iWebPlus QQ module, its manifest and corresponding source."""

import concurrent.futures
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "iwebplus"
CONFIG = json.loads((MODULE / "build.json").read_text())
COMMIT = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
EPOCH = int(subprocess.check_output(["git", "-C", ROOT, "show", "-s", "--format=%ct", "HEAD"], text=True))
os.environ["SOURCE_DATE_EPOCH"] = str(EPOCH)


def digest(path):
    """Return the lowercase SHA-256 of a file."""
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    """Write stable, human-readable JSON."""
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(*args, **options):
    """Run a command and echo its argv."""
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **options)


def download(url, sha, target):
    """Download a pinned source distribution and verify its digest."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and digest(target) == sha:
        return target
    temporary = target.with_suffix(target.suffix + ".part")
    run(
        "curl",
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--output",
        temporary,
        url,
    )
    if digest(temporary) != sha:
        raise RuntimeError("Download SHA256 mismatch: " + target.name)
    temporary.replace(target)
    return target


def extract(archive, destination):
    """Extract a trusted source archive without unsafe members."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")


def package_sources(packages, licenses, work, dist):
    """Create the complete corresponding source archive for this commit."""
    tree = work / "corresponding-source"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    # The tagged commit already contains upstream, launcher, route patch and tests.
    source = tree / f"qqmusic-source-{CONFIG['version']}"
    source.mkdir()
    archive_path = tree / "commit.tar"
    run("git", "-C", ROOT, "archive", "--format=tar", f"--prefix={source.name}/", "--output", archive_path, "HEAD")
    with tarfile.open(archive_path) as archive:
        archive.extractall(tree, filter="data")
    archive_path.unlink()
    # Keep the exact commit rebuildable from the archive without GitHub.
    run("git", "-C", ROOT, "bundle", "create", source / "fork.bundle", "HEAD")
    shutil.copytree(licenses, source / "licenses")
    shutil.copyfile(work / "system-libraries.json", source / "system-libraries.json")
    write_json(source / "source-identity.json", {"commit": COMMIT, "python": sys.version, "sourceEpoch": EPOCH})
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    sources = {(item["name"], item.get("version")): item["sdist"] for item in locked["package"] if "sdist" in item}
    tools = {
        (item["name"], item["version"]): item for item in json.loads((MODULE / "build-tool-sources.json").read_text())
    }
    entries = []
    for package in packages:
        pair = (package["name"], package["version"])
        item = sources.get(pair) or tools.get(pair)
        if item is None:
            raise RuntimeError("Missing corresponding source: " + str(pair))
        entries.append(
            {
                "name": pair[0],
                "version": pair[1],
                "url": item["url"],
                "sha256": item.get("sha256") or item["hash"].removeprefix("sha256:"),
            }
        )

    def fetch_source(entry):
        archive = download(
            entry["url"],
            entry["sha256"],
            work / "downloads" / (entry["name"] + "-" + entry["version"] + ".tar.gz"),
        )
        extract(archive, source / "dependencies" / (entry["name"] + "-" + entry["version"]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch_source, entries))
    write_json(source / "dependency-sources.json", entries)

    def normalized(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
        return info

    target = dist / f"qqmusic-source-{CONFIG['version']}.tar.gz"
    with (
        target.open("wb") as stream,
        gzip.GzipFile(
            filename="", fileobj=stream, mode="wb", mtime=int(os.environ.get("SOURCE_DATE_EPOCH", "0")), compresslevel=9
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        archive.add(source, arcname=source.name, filter=normalized)
    return target


def collect_system_licenses(licenses, work):
    """Retain notices and exact source-package identities for bundled system libraries."""
    packages = {"python3.12"}
    libraries = []
    for name, filename, kind in json.loads((work / "libraries.json").read_text()):
        path = Path(filename).resolve()
        if kind not in ("BINARY", "EXTENSION") or not str(path).startswith("/usr/lib/"):
            continue
        owner = subprocess.check_output(["dpkg-query", "-S", str(path)], text=True)
        package = owner.splitlines()[-1].split(": ", 1)[0]
        packages.add(package)
        libraries.append({"name": name, "sha256": digest(path), "package": package})
    records = []
    for package in sorted(packages):
        record = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${source:Package}\\t${source:Version}", package],
            text=True,
        ).split("\t")
        target = licenses / "system" / record[0]
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path("/usr/share/doc") / record[0] / "copyright", target / "copyright")
        records.append(dict(zip(("package", "version", "sourcePackage", "sourceVersion"), record, strict=True)))
    write_json(work / "system-libraries.json", {"packages": records, "libraries": libraries})
    shutil.copyfile(work / "system-libraries.json", licenses / "system-libraries.json")


def main():
    """Run the pinned build and emit release assets."""
    if subprocess.check_output(["git", "-C", ROOT, "status", "--porcelain"], text=True).strip():
        raise RuntimeError("Release builds require a clean committed tree")
    if Path(sys.base_prefix) != Path("/usr") or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Build requires the Ubuntu system Python 3.12")
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or platform.libc_ver() != ("glibc", CONFIG["minGlibc"])
    ):
        raise RuntimeError(f"Build requires Linux x64 and glibc {CONFIG['minGlibc']}")
    work = Path(os.environ.get("IWEBPLUS_WORK", ROOT / "build/iwebplus"))
    dist = Path(os.environ.get("IWEBPLUS_DIST", ROOT / "dist"))
    work.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    launcher = (MODULE / "launcher.py").read_text()
    if f'VERSION = "{CONFIG["version"]}"' not in launcher:
        raise RuntimeError("launcher.py VERSION does not match build.json")
    python = work / "venv/bin/python"
    env = dict(
        os.environ,
        UV_PROJECT_ENVIRONMENT=str(work / "venv"),
        UV_PYTHON_DOWNLOADS="never",
        UV_NO_PROGRESS="1",
        UV_LINK_MODE="copy",
        TMPDIR=str(work / "tmp"),
        TMP=str(work / "tmp"),
        TEMP=str(work / "tmp"),
        SOURCE_DATE_EPOCH=str(EPOCH),
        IWEBPLUS_LIBRARIES=str(work / "libraries.json"),
    )
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"):
        env.pop(name, None)
    (work / "tmp").mkdir(exist_ok=True)
    run(
        uv,
        "sync",
        "--frozen",
        "--no-default-groups",
        "--group",
        "web",
        "--no-install-project",
        "--no-build",
        "--python",
        sys.executable,
        cwd=ROOT,
        env=env,
    )
    run(
        uv,
        "pip",
        "install",
        "--python",
        python,
        "--no-deps",
        "--require-hashes",
        "-r",
        MODULE / "build-requirements.txt",
        env=env,
    )
    run(
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        work / "dist",
        "--workpath",
        work / "pyinstaller",
        MODULE / "qqmusic.spec",
        cwd=ROOT,
        env=env,
    )
    binary = work / "dist/qqmusic"
    sha = digest(binary)
    filename = f"qqmusic-linux-x64-{CONFIG['version']}-{sha[:16]}"
    target = dist / filename
    shutil.copyfile(binary, target)
    target.chmod(0o755)
    licenses = dist / "licenses"
    if licenses.exists():
        shutil.rmtree(licenses)
    shutil.copytree(MODULE / "licenses", licenses)
    packages = json.loads(
        run(python, MODULE / "collect-licenses.py", licenses, cwd=ROOT, env=env, text=True, capture_output=True).stdout
    )
    write_json(licenses / "packages.json", packages)
    collect_system_licenses(licenses, work)
    source_archive = package_sources(packages, licenses, work, dist)
    manifest = {
        "protocol": 1,
        "version": CONFIG["version"],
        "upstreamVersion": CONFIG["upstreamVersion"],
        "os": "linux",
        "arch": "x64",
        "libc": "glibc",
        "minGlibc": CONFIG["minGlibc"],
        "filename": filename,
        "sha256": sha,
        "size": binary.stat().st_size,
        "sourceArchive": source_archive.name,
        "sourceSha256": digest(source_archive),
        "sourceCommit": COMMIT,
    }
    write_json(dist / "qq-module.json", manifest)
    licenses_archive = dist / f"qqmusic-licenses-{CONFIG['version']}.tar.gz"
    with (
        licenses_archive.open("wb") as stream,
        gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=EPOCH, compresslevel=9) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for item in sorted(licenses.rglob("*")):
            if item.is_file():
                archive.add(item, arcname=str(Path("licenses") / item.relative_to(licenses)))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
