// SPDX-License-Identifier: GPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const { execFile, spawn, execFileSync } = require('node:child_process');
const { promisify } = require('node:util');
const exec = promisify(execFile);
const root = path.resolve(__dirname, '../..');
const config = JSON.parse(fs.readFileSync(path.join(root, 'iwebplus/build.json'), 'utf8'));
const output = path.resolve(process.env.IWEBPLUS_DIST || path.join(root, 'dist'));
const manifestPath = path.join(output, 'qq-module.json');
const sha = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function alive(pid) {
    try {
        const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
        return stat.slice(stat.lastIndexOf(')') + 2).split(' ')[0] !== 'Z';
    } catch (error) {
        if (error.code === 'ENOENT') return false;
        throw error;
    }
}

async function gone(pid) {
    for (let i = 0; i < 120; i++) {
        if (!alive(pid)) return;
        await delay(50);
    }
    assert.fail(`owned test process ${pid} did not exit`);
}

function request(ready, headers = {}, route = '/_iwebplus/health', method = 'GET') {
    return new Promise((resolve, reject) => {
        const req = http.request({ host: '127.0.0.1', port: ready.port, path: route, method, headers }, response => {
            const data = [];
            response.on('data', chunk => data.push(chunk));
            response.on('end', () => resolve({
                status: response.statusCode, headers: response.headers, text: Buffer.concat(data).toString()
            }));
        });
        req.setTimeout(5000, () => req.destroy(new Error('HTTP timeout')));
        req.on('error', reject);
        req.end();
    });
}

test('real QQ onefile runtime, ownership and HTTP boundary', {
    timeout: 300000
}, async t => {
    // Missing artifacts are failures locally and in CI, never an unexecuted green test.
    assert.equal(process.platform, 'linux', 'the real onefile lifecycle requires Linux');
    assert.ok(fs.existsSync(manifestPath), `missing ${manifestPath}; build iwebplus/build.py first`);
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    const binary = path.join(output, manifest.filename);
    assert.ok(fs.existsSync(binary), `missing executable: ${binary}`);
    const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'qq-runtime-test-'));
    fs.mkdirSync(path.join(cwd, 'tmp'), { mode: 0o700 });
    const env = { ...process.env, PATH: '', PYINSTALLER_RESET_ENVIRONMENT: '1',
        TMPDIR: path.join(cwd, 'tmp'), TMP: path.join(cwd, 'tmp'), TEMP: path.join(cwd, 'tmp') };
    for (const key of ['PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'LD_AUDIT']) delete env[key];
    const helper = async (...args) => {
        const result = await exec(binary, args, { cwd, env, timeout: 65000, maxBuffer: 2 * 1024 * 1024 });
        assert.equal(result.stderr, '', `unexpected helper stderr: ${result.stderr}`);
        assert.equal(result.stdout.trim().split('\n').length, 1);
        return JSON.parse(result.stdout);
    };
    const rejected = async (...args) => {
        await assert.rejects(helper(...args), error => {
            assert.equal(error.code, 1);
            assert.equal(error.stdout, '');
            assert.match(JSON.parse(error.stderr).error, /^qq-module: /);
            return true;
        });
    };
    const servers = [];
    const sessions = [];
    async function launch() {
        const prepared = await helper('prepare');
        sessions.push(prepared);
        const processEnv = { ...env, IWEBPLUS_RUN_DIR: prepared.runDir,
            IWEBPLUS_MODULE_NONCE: prepared.nonce, IWEBPLUS_MODULE_TOKEN: prepared.token,
            IWEBPLUS_MODULE_VERSION: config.version,
            TMPDIR: prepared.runDir + '/tmp', TMP: prepared.runDir + '/tmp', TEMP: prepared.runDir + '/tmp',
            QQMUSIC_SERVER: '{"host":"0.0.0.0","port":8080}',
            QQMUSIC_CREDENTIAL: '{"enabled":true,"store":{"path":"/not-allowed.sqlite3"}}',
            QQMUSIC_SECURITY: '{"cors_enabled":true,"cors_allow_origins":["*"]}',
            PYTHON_ZSTANDARD_IMPORT_POLICY: 'cffi' };
        const child = spawn(binary, ['serve'], { cwd, env: processEnv, stdio: ['ignore', 'pipe', 'pipe'] });
        let logs = '';
        child.stdout.on('data', value => { logs += value; });
        child.stderr.on('data', value => { logs += value; });
        const item = { prepared, child, logs: () => logs };
        servers.push(item);
        item.ready = await helper('wait-ready', prepared.runDir);
        return item;
    }
    try {
        let first;
        await t.test('manifest, source and licenses describe the actual bytes', () => {
            assert.ok(manifest);
            assert.equal(manifest.protocol, 1);
            for (const key of ['version', 'upstreamVersion', 'minGlibc']) {
                assert.equal(manifest[key], config[key], key);
            }
            assert.equal(manifest.os, 'linux');
            assert.equal(manifest.arch, 'x64');
            assert.equal(manifest.libc, 'glibc');
            const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
            assert.equal(manifest.sourceCommit, commit);
            assert.equal(manifest.filename, `qqmusic-linux-x64-${manifest.version}-${sha(binary).slice(0, 16)}`);
            assert.equal(manifest.sha256, sha(binary));
            assert.equal(manifest.size, fs.statSync(binary).size);
            assert.equal(manifest.sourceArchive, `qqmusic-source-${config.version}.tar.gz`);
            const archive = path.join(output, manifest.sourceArchive);
            assert.equal(manifest.sourceSha256, sha(archive));
            const files = execFileSync('tar', ['tzf', archive], {
                maxBuffer: 64 * 1024 * 1024, encoding: 'utf8'
            }).trim().split('\n');
            const sourceRoot = `qqmusic-source-${manifest.version}`;
            const members = new Set(files);
            for (const name of ['iwebplus/launcher.py', 'iwebplus/tests/test_login.py',
                'iwebplus/tests/test_routes.py', 'iwebplus/tests/test_runtime_diagnostics.py',
                'iwebplus/tests/conftest.py', 'iwebplus/tests/runtime.test.cjs',
                'iwebplus/build.py', 'iwebplus/build.json', 'iwebplus/qqmusic.spec', 'uv.lock',
                'web/src/app.py', 'web/src/routes/report.py', 'qqmusic_api/__init__.py',
                'dependency-sources.json', 'licenses/NOTICE.txt', 'licenses/GPL-3.0-or-later.txt',
                'system-libraries.json', 'source-identity.json', 'fork.bundle']) {
                assert.ok(members.has(`${sourceRoot}/${name}`), 'missing source: ' + name);
            }
            const source = name => execFileSync('tar', ['xOf', archive, `${sourceRoot}/${name}`], {
                maxBuffer: 16 * 1024 * 1024
            });
            assert.deepEqual(source('uv.lock'), fs.readFileSync(path.join(root, 'uv.lock')));
            assert.deepEqual(JSON.parse(source('iwebplus/build.json')), config);
            const identity = JSON.parse(source('source-identity.json'));
            assert.equal(identity.commit, commit);
            assert.equal(identity.sourceEpoch, Number(execFileSync('git', ['show', '-s', '--format=%ct', commit], {
                cwd: root, encoding: 'utf8'
            }).trim()));
            assert.match(identity.python, /^3\.12\./);
            const bundle = path.join(cwd, 'fork.bundle');
            // The bundle may include the full fork history; stream it rather than cap its size.
            const descriptor = fs.openSync(bundle, 'w', 0o600);
            try {
                execFileSync('tar', ['xOf', archive, `${sourceRoot}/fork.bundle`], {
                    stdio: ['ignore', descriptor, 'pipe']
                });
            } finally {
                fs.closeSync(descriptor);
            }
            assert.ok(execFileSync('git', ['bundle', 'list-heads', bundle], { encoding: 'utf8' })
                .trim().split('\n').includes(`${commit} HEAD`));
            const singer = source('qqmusic_api/modules/singer.py').toString();
            assert.match(singer, /def get_mv_tag\(/);
            assert.match(singer, /filter_type: AlbumFilterType/);
            const sources = JSON.parse(source('dependency-sources.json'));
            const packages = JSON.parse(fs.readFileSync(path.join(output, 'licenses/packages.json')));
            for (const name of ['pyinstaller', 'cryptography']) {
                assert.ok(packages.some(item => item.name === name), `missing bundled dependency: ${name}`);
            }
            for (const item of packages) {
                const entry = sources.find(value => value.name === item.name && value.version === item.version);
                assert.ok(entry, `missing dependency source: ${item.name}@${item.version}`);
                assert.match(entry.sha256, /^[a-f0-9]{64}$/);
                assert.ok(files.some(name => name.startsWith(
                    `${sourceRoot}/dependencies/${item.name}-${item.version}/`) && !name.endsWith('/')));
            }
            assert.deepEqual(JSON.parse(source('system-libraries.json')),
                JSON.parse(fs.readFileSync(path.join(output, 'licenses/system-libraries.json'))));
            assert.ok(!files.some(name => /\/(?:\.venv|runtime\/qq)\//.test(name)));
            assert.ok(fs.existsSync(path.join(output, 'licenses/GPL-3.0-or-later.txt')));
            const linked = execFileSync('ldd', [binary], { encoding: 'utf8' });
            assert.doesNotMatch(linked, /libpython|not found/);
        });
        await t.test('prepare creates the host storage directory with mode 0700', async () => {
            const data = path.join(cwd, 'data');
            assert.ok(!fs.existsSync(data));
            const prepared = await helper('prepare');
            sessions.push(prepared);
            assert.ok(fs.lstatSync(data).isDirectory());
            assert.equal(fs.statSync(data).mode & 0o777, 0o700);
            await helper('clean', prepared.runDir);
        });
        await t.test('prepare tightens owned data 0755 without rewriting a dummy 0644 session', async () => {
            const data = path.join(cwd, 'data');
            fs.chmodSync(data, 0o755);
            const dummy = path.join(data, 'dummy_session');
            fs.writeFileSync(dummy, 'test fixture, not an account credential');
            fs.chmodSync(dummy, 0o644);
            const directoryInode = fs.statSync(data).ino;
            const sessionInode = fs.statSync(dummy).ino;
            const prepared = await helper('prepare');
            sessions.push(prepared);
            assert.equal(fs.statSync(data).mode & 0o777, 0o700);
            assert.equal(fs.statSync(data).ino, directoryInode);
            assert.equal(fs.statSync(dummy).mode & 0o777, 0o644);
            assert.equal(fs.statSync(dummy).ino, sessionInode);
            assert.equal(fs.readFileSync(dummy, 'utf8'), 'test fixture, not an account credential');
            await helper('clean', prepared.runDir);
        });
        await t.test('prepare refuses a data symlink without changing its target or creating a run', async () => {
            const data = path.join(cwd, 'data');
            const held = path.join(cwd, 'data-held');
            const target = path.join(cwd, 'symlink-target');
            fs.mkdirSync(target);
            fs.chmodSync(target, 0o755);
            const runs = path.join(cwd, 'runtime/qq');
            const before = fs.readdirSync(runs).sort();
            fs.renameSync(data, held);
            fs.symlinkSync(target, data);
            try {
                await rejected('prepare');
                assert.ok(fs.lstatSync(data).isSymbolicLink());
                assert.equal(fs.statSync(target).mode & 0o777, 0o755);
                assert.deepEqual(fs.readdirSync(target), []);
                assert.deepEqual(fs.readdirSync(runs).sort(), before);
            } finally {
                fs.unlinkSync(data);
                fs.renameSync(held, data);
            }
        });
        await t.test('starts without Python on PATH; reports actual child and bootloader PIDs', async () => {
            first = await launch();
            const { prepared, ready, child } = first;
            assert.match(prepared.nonce, /^[a-f0-9]{32}$/);
            assert.match(prepared.token, /^[a-f0-9]{64}$/);
            assert.equal(prepared.runDir, path.join(cwd, 'runtime/qq', prepared.nonce));
            assert.equal(fs.statSync(prepared.runDir).mode & 0o777, 0o700);
            assert.equal(fs.statSync(prepared.runDir + '/tmp').mode & 0o777, 0o700);
            assert.deepEqual(Object.keys(ready).sort(), ['nonce', 'pid', 'port', 'protocol', 'supervisorPid', 'version']);
            assert.equal(ready.protocol, 1);
            assert.equal(ready.nonce, prepared.nonce);
            assert.equal(ready.version, config.version);
            assert.equal(ready.supervisorPid, child.pid);
            assert.notEqual(ready.pid, child.pid);
            assert.ok(alive(ready.pid) && alive(ready.supervisorPid));
            for (const name of ['session.json', 'process.json', 'ready.json']) {
                const file = path.join(prepared.runDir, name);
                assert.equal(fs.statSync(file).mode & 0o777, 0o600);
                assert.ok(!fs.readFileSync(file, 'utf8').includes(prepared.token));
            }
            const portHex = ready.port.toString(16).toUpperCase().padStart(4, '0');
            const sockets = fs.readFileSync('/proc/net/tcp', 'utf8').split('\n')
                .filter(line => line.includes(':' + portHex + ' ') && line.trim().split(/\s+/)[3] === '0A');
            assert.equal(sockets.length, 1);
            assert.equal(sockets[0].trim().split(/\s+/)[1], '0100007F:' + portHex);
        });
        if (!first?.ready) throw new Error('runtime did not start');
        const { prepared, ready } = first;
        const authorization = { Authorization: 'Bearer ' + prepared.token };
        await t.test('all HTTP routes, health and errors require exactly one Bearer token', async () => {
            for (const route of ['/_iwebplus/health', '/openapi.json', '/docs', '/', '/missing',
                '/login/qrcode/qq', '/_iwebplus/health?token=' + prepared.token]) {
                assert.equal((await request(ready, {}, route)).status, 401, route);
                assert.equal((await request(ready, { Authorization: 'Bearer ' + '0'.repeat(64) }, route)).status, 401);
            }
            assert.equal((await request(ready, { Cookie: 'token=' + prepared.token })).status, 401);
            assert.equal((await request(ready, [
                'Host', `127.0.0.1:${ready.port}`, 'Authorization', 'Bearer ' + prepared.token,
                'Authorization', 'Bearer ' + prepared.token
            ])).status, 401);
            const response = await request(ready, authorization);
            assert.equal(response.status, 200);
            assert.deepEqual(JSON.parse(response.text), { ...ready, ok: true });
            assert.equal(response.headers['cache-control'], 'no-store');
            assert.ok(!response.text.includes(prepared.token));
        });
        await t.test('fork OpenAPI and business validation work; no redirects or CORS', async () => {
            const response = await request(ready, authorization, '/openapi.json');
            assert.equal(response.status, 200);
            const schema = JSON.parse(response.text);
            assert.equal(schema.info.title, 'QQMusic API');
            assert.equal(schema.info.version, config.upstreamVersion);
            for (const route of ['/login/qrcode/{login_type}', '/songlist/{songlist_id}/detail',
                '/user/get_vip_info', '/search/search_by_type', '/song/query_song', '/song/{value}/lyric',
                '/song/{mid}/url', '/song/get_song_urls', '/recommend/get_guess_recommend',
                '/recommend/get_radar_recommend', '/sound_power/detail', '/sound_power/friend_rank',
                '/report/play_duration']) {
                assert.ok(schema.paths[route], route);
            }
            // The version string alone cannot prove that the executable contains the fork routes.
            for (const [route, name] of [
                ['/singer/{mid}/albums', 'filter_type'], ['/singer/{mid}/mvs', 'tag_id'],
                ['/song/{value}/lyric', 'qrc'], ['/song/{value}/lyric', 'trans']
            ]) assert.ok(schema.paths[route].get.parameters.some(item => item.name === name), `${route}: ${name}`);
            assert.equal(schema.components.schemas.GetSongUrlsResponse.properties.data.type, 'array');
            assert.equal(schema.components.schemas.UrlinfoItem.properties.purl.type, 'string');
            assert.equal(schema.components.schemas.GetLyricResponse.properties.trans.type, 'string');
            const radar = schema.paths['/recommend/get_radar_recommend'].get;
            assert.deepEqual(radar.security, [{ MusicId: [], MusicKey: [] }]);
            assert.equal(radar.parameters.find(item => item.name === 'page').schema.default, 1);
            assert.equal(radar.responses['200'].content['application/json'].schema.$ref,
                '#/components/schemas/ApiResponse_RadarRecommendResponse_');
            const report = schema.paths['/report/play_duration'];
            assert.deepEqual(Object.keys(report), ['post']);
            assert.deepEqual(report.post.security, [{ MusicId: [], MusicKey: [] }]);
            assert.deepEqual(report.post.parameters.map(item => [item.name, item.in, item.schema.type]).sort(),
                [['listened_seconds', 'query', 'integer'], ['songmid', 'query', 'string']]);
            assert.equal(report.post.responses['200'].content['application/json'].schema.$ref,
                '#/components/schemas/ApiResponse_NoneType_');
            assert.equal((await request(ready, authorization, '/')).status, 200);
            assert.equal((await request(ready, authorization, '/songlist/not-an-integer/detail')).status, 422);
            const slash = await request(ready, authorization, '/openapi.json/');
            assert.equal(slash.status, 404);
            assert.equal(slash.headers.location, undefined);
            for (const method of ['GET', 'OPTIONS']) {
                const cross = await request(ready, { ...authorization, Origin: 'https://evil.example' }, '/', method);
                assert.equal(cross.status, 403);
                assert.ok(!Object.keys(cross.headers).some(name => name.startsWith('access-control-')));
            }
            assert.equal((await request(ready, { ...authorization, Host: 'evil.example' })).status, 400);
            assert.equal((await request(ready, authorization, '/_iwebplus/health', 'POST')).status, 405);
        });
        await t.test('radar rejects guest and incomplete credentials locally before any upstream request', async () => {
            const route = '/recommend/get_radar_recommend?page=2';
            const guest = await request(ready, authorization, route);
            assert.equal(guest.status, 401);
            assert.equal(JSON.parse(guest.text).code, -1);
            assert.equal(guest.headers['cache-control'], 'no-store');
            const incomplete = await request(ready, { ...authorization, Cookie: 'musicid=101' }, route);
            assert.equal(incomplete.status, 422);
            assert.equal(JSON.parse(incomplete.text).code, -1);
        });
        await t.test('authenticated local batches are not subject to the upstream 60/min IP limit', async () => {
            // Exercise upstream security middleware, not the wrapper-only health
            // route. 130 requests also cover a batch straddling a minute boundary.
            const start = performance.now();
            for (let i = 0; i < 130; i++) {
                const response = await request(ready, authorization, '/missing-qq-runtime-rate-check');
                assert.equal(response.status, 404, `local request ${i + 1} returned ${response.status}`);
            }
            assert.ok(performance.now() - start < 60000, 'batch must complete inside one minute');
        });
        await t.test('wrong nonce, PID, forged identity and active cleanup are refused without signals', async () => {
            await rejected('clean', prepared.runDir);
            await rejected('stop', String(ready.supervisorPid), prepared.runDir, '0'.repeat(32));
            await rejected('stop', String(process.pid), prepared.runDir, prepared.nonce);
            const file = path.join(prepared.runDir, 'process.json');
            const original = fs.readFileSync(file);
            const forged = JSON.parse(original);
            forged.processes[0].startTicks = '0';
            fs.writeFileSync(file, JSON.stringify(forged));
            try { await rejected('stop', String(ready.supervisorPid), prepared.runDir, prepared.nonce); }
            finally { fs.writeFileSync(file, original); }
            assert.ok(alive(ready.pid) && alive(ready.supervisorPid) && alive(process.pid));
        });
        await t.test('path traversal, symlink directories and ready-file symlinks are refused', async () => {
            await rejected('clean', cwd);
            await rejected('clean', prepared.runDir + '/../' + prepared.nonce);
            const link = path.join(cwd, 'runtime/qq', 'f'.repeat(32));
            fs.symlinkSync(prepared.runDir, link);
            await rejected('clean', link);
            fs.unlinkSync(link);
            const file = path.join(prepared.runDir, 'ready.json');
            const original = fs.readFileSync(file);
            const outside = path.join(cwd, 'outside.json');
            fs.writeFileSync(outside, original, { mode: 0o600 });
            fs.unlinkSync(file);
            fs.symlinkSync(outside, file);
            try { await rejected('wait-ready', prepared.runDir); }
            finally { fs.unlinkSync(file); fs.writeFileSync(file, original, { mode: 0o600 }); }
            assert.ok(alive(ready.pid));
        });
        await t.test('stop reaps both onefile processes, is idempotent, and clean does not follow links', async () => {
            assert.deepEqual(await helper('stop', String(ready.supervisorPid), prepared.runDir, prepared.nonce), { ok: true });
            await gone(ready.pid);
            await gone(ready.supervisorPid);
            assert.deepEqual(await helper('stop', String(ready.pid), prepared.runDir, prepared.nonce), { ok: true });
            assert.ok(!fs.existsSync(path.join(prepared.runDir, 'ready.json')));
            const sentinel = path.join(cwd, 'keep-me');
            fs.writeFileSync(sentinel, 'outside runtime');
            fs.symlinkSync(sentinel, path.join(prepared.runDir, 'outside-link'));
            assert.deepEqual(await helper('clean', prepared.runDir), { ok: true });
            assert.equal(fs.readFileSync(sentinel, 'utf8'), 'outside runtime');
            assert.ok(!fs.existsSync(prepared.runDir));
            assert.ok(!first.logs().includes(prepared.token));
        });
        await t.test('host killing only bootloader cannot orphan the HTTP service', async () => {
            const second = await launch();
            second.child.kill('SIGKILL');
            await gone(second.ready.pid);
            await gone(second.ready.supervisorPid);
            await helper('stop', String(second.ready.supervisorPid), second.prepared.runDir, second.prepared.nonce);
            await helper('clean', second.prepared.runDir);
            assert.ok(!second.logs().includes(second.prepared.token));
        });
        await t.test('SIGKILL escalation is limited to a verified hung child', async () => {
            const third = await launch();
            process.kill(third.ready.pid, 'SIGSTOP');
            await helper('stop', String(third.ready.supervisorPid), third.prepared.runDir, third.prepared.nonce);
            await gone(third.ready.pid);
            await gone(third.ready.supervisorPid);
            await helper('clean', third.prepared.runDir);
            assert.ok(alive(process.pid));
        });
        await t.test('wait-ready times out within 60 seconds instead of guessing a port', async () => {
            const pending = await helper('prepare');
            sessions.push(pending);
            const start = performance.now();
            await rejected('wait-ready', pending.runDir);
            const elapsed = performance.now() - start;
            assert.ok(elapsed >= 59500 && elapsed < 64000, `elapsed ${elapsed} ms includes onefile bootstrap`);
            await helper('clean', pending.runDir);
        });
    } finally {
        for (const server of servers) {
            if (alive(server.child.pid)) {
                try { await helper('stop', String(server.child.pid), server.prepared.runDir, server.prepared.nonce); }
                catch (_) { server.child.kill('SIGKILL'); }
            }
        }
        for (const item of sessions) {
            if (fs.existsSync(item.runDir)) {
                try { await helper('clean', item.runDir); } catch (_) {}
            }
        }
        // Never remove a live runtime in a failed test.
        if (!servers.some(item => alive(item.child.pid) || (item.ready && alive(item.ready.pid)))) {
            fs.rmSync(cwd, { recursive: true, force: true });
        }
    }
});
