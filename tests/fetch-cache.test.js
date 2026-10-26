const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const run = require('../fetch-cache.js');
const { pickVersion, archiveExtension } = run;

const entry = (over = {}) => ({ key: 'k', ref: 'refs/heads/main', version: 'v1', ...over });

test('pickVersion prefers the run ref, then the default branch', () => {
  const caches = [entry(), entry({ ref: 'refs/heads/dev', version: 'v2' }), entry({ key: 'k2', version: 'v3' })];
  assert.strictEqual(pickVersion(caches, 'k', 'refs/heads/dev', 'refs/heads/main'), 'v2');
  assert.strictEqual(pickVersion(caches, 'k', 'refs/heads/other', 'refs/heads/main'), 'v1');
});

test('pickVersion rejects unknown keys, unreadable refs and ambiguous versions', () => {
  assert.throws(() => pickVersion([entry({ key: 'kk' })], 'k', 'refs/heads/main', 'refs/heads/main'), /No cache entry/);
  assert.throws(() => pickVersion([entry({ ref: 'refs/heads/x' })], 'k', 'refs/heads/y', 'refs/heads/main'), /cannot read/);
  assert.throws(() => pickVersion([entry(), entry({ version: 'v9' })], 'k', 'refs/heads/main', 'refs/heads/main'), /2 versions/);
});

test('archiveExtension detects zstd, gzip and plain tar', () => {
  assert.strictEqual(archiveExtension(Buffer.from([0x28, 0xb5, 0x2f, 0xfd])), '.tzst');
  assert.strictEqual(archiveExtension(Buffer.from([0x1f, 0x8b, 8, 0])), '.tgz');
  assert.strictEqual(archiveExtension(Buffer.from('abc')), '.tar');
});

function fakeFetch(calls, { ok = true, archive = Buffer.from([0x28, 0xb5, 0x2f, 0xfd, 1, 2, 3]) } = {}) {
  return async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).includes('GetCacheEntryDownloadURL')) {
      return new Response(JSON.stringify({ ok, signed_download_url: 'https://blob.example/x?sig=1', matched_key: 'k' }), { status: 200 });
    }
    return new Response(archive, { status: 200 });
  };
}

function fakeCore() {
  const outputs = {};
  return { outputs, info() {}, setOutput: (k, v) => { outputs[k] = v; } };
}

const github = {
  rest: { actions: { getActionsCacheList: 'list' } },
  paginate: async (method, params) => {
    assert.strictEqual(method, 'list');
    assert.deepStrictEqual([params.owner, params.repo, params.key], ['o', 'r', 'k']);
    return [entry({ version: 'looked-up' })];
  },
};
const context = { repo: { owner: 'o', repo: 'r' }, ref: 'refs/heads/main', payload: { repository: { default_branch: 'main' } } };

test('run looks up the version, requests the URL and saves the archive', async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'fc-'));
  const calls = [];
  const core = fakeCore();
  const env = { CACHE_KEY: 'k', ACTIONS_RESULTS_URL: 'https://results.example/', ACTIONS_RUNTIME_TOKEN: 'tok', RUNNER_TEMP: tmp };
  await run({ github, context, core, fetchImpl: fakeFetch(calls), env });

  assert.strictEqual(calls[0].url, 'https://results.example/twirp/github.actions.results.api.v1.CacheService/GetCacheEntryDownloadURL');
  assert.strictEqual(calls[0].init.headers.Authorization, 'Bearer tok');
  assert.deepStrictEqual(JSON.parse(calls[0].init.body), { key: 'k', version: 'looked-up' });
  assert.strictEqual(calls[1].url, 'https://blob.example/x?sig=1');
  assert.ok(core.outputs['archive-path'].endsWith('cache.tzst'));
  assert.strictEqual(fs.readFileSync(core.outputs['archive-path']).length, 7);
  assert.strictEqual(core.outputs.size, '7');
  fs.rmSync(tmp, { recursive: true });
});

test('run skips the lookup when a version is given and reports a miss', async () => {
  const calls = [];
  const env = { CACHE_KEY: 'k', CACHE_VERSION: 'given', ACTIONS_RESULTS_URL: 'https://r/', ACTIONS_RUNTIME_TOKEN: 't', RUNNER_TEMP: os.tmpdir() };
  const noLookup = { paginate: () => assert.fail('lookup should be skipped') };
  await assert.rejects(
    run({ github: noLookup, context, core: fakeCore(), fetchImpl: fakeFetch(calls, { ok: false }), env }),
    /found no entry/
  );
  assert.deepStrictEqual(JSON.parse(calls[0].init.body), { key: 'k', version: 'given' });
});
