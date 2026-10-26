// Fetches the raw archive of a GitHub Actions cache entry from inside a workflow run.
// Loaded by action.yml through actions/github-script, which passes the runtime token
// (ACTIONS_RUNTIME_TOKEN) and the cache service URL (ACTIONS_RESULTS_URL) to node steps.
const fs = require('fs');
const path = require('path');
const { Readable } = require('stream');
const { pipeline } = require('stream/promises');

const DOWNLOAD_URL_METHOD =
  '/twirp/github.actions.results.api.v1.CacheService/GetCacheEntryDownloadURL';

// The REST list endpoint matches keys by prefix, so only exact matches are kept.
// A run can read caches of its own ref and of the default branch.
function pickVersion(caches, key, ref, defaultRef) {
  const exact = caches.filter((c) => c.key === key);
  if (exact.length === 0) {
    throw new Error(`No cache entry with key "${key}" exists in this repository.`);
  }
  const readable = exact.filter((c) => c.ref === ref || c.ref === defaultRef);
  if (readable.length === 0) {
    const refs = [...new Set(exact.map((c) => c.ref))].join(', ');
    throw new Error(
      `Cache "${key}" exists only for ${refs}, which this run (${ref}) cannot read. ` +
        'Run the workflow on that branch.'
    );
  }
  const own = readable.filter((c) => c.ref === ref);
  const pool = own.length > 0 ? own : readable;
  const versions = [...new Set(pool.map((c) => c.version))];
  if (versions.length > 1) {
    throw new Error(
      `Cache "${key}" has ${versions.length} versions on ${pool[0].ref} ` +
        `(${versions.join(', ')}). Pass one as the version input.`
    );
  }
  return versions[0];
}

function archiveExtension(head) {
  if (head.length >= 4 && head.readUInt32LE(0) === 0xfd2fb528) return '.tzst';
  if (head.length >= 2 && head[0] === 0x1f && head[1] === 0x8b) return '.tgz';
  return '.tar';
}

async function getDownloadUrl({ key, version, resultsUrl, runtimeToken, fetchImpl }) {
  const res = await fetchImpl(new URL(DOWNLOAD_URL_METHOD, resultsUrl), {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${runtimeToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ key, version }),
  });
  if (!res.ok) {
    throw new Error(`GetCacheEntryDownloadURL failed: HTTP ${res.status} ${await res.text()}`);
  }
  const body = await res.json();
  if (!body.ok) {
    throw new Error(
      `The cache service found no entry for key "${key}" and version ${version} ` +
        'that this run can read.'
    );
  }
  return body.signed_download_url;
}

async function run({ github, context, core, fetchImpl = fetch, env = process.env }) {
  const key = env.CACHE_KEY;
  let version = env.CACHE_VERSION;
  if (!key) throw new Error('The key input is empty.');
  if (!env.ACTIONS_RESULTS_URL || !env.ACTIONS_RUNTIME_TOKEN) {
    throw new Error('ACTIONS_RESULTS_URL or ACTIONS_RUNTIME_TOKEN is not set; run this inside GitHub Actions.');
  }

  if (!version) {
    const { owner, repo } = context.repo;
    const caches = await github.paginate(github.rest.actions.getActionsCacheList, {
      owner,
      repo,
      key,
      per_page: 100,
    });
    const defaultRef = `refs/heads/${context.payload.repository?.default_branch ?? ''}`;
    version = pickVersion(caches, key, context.ref, defaultRef);
  }
  core.info(`Cache key: ${key}`);
  core.info(`Cache version: ${version}`);

  const url = await getDownloadUrl({
    key,
    version,
    resultsUrl: env.ACTIONS_RESULTS_URL,
    runtimeToken: env.ACTIONS_RUNTIME_TOKEN,
    fetchImpl,
  });

  const dir = fs.mkdtempSync(path.join(env.RUNNER_TEMP || require('os').tmpdir(), 'cache-'));
  const partial = path.join(dir, 'cache.partial');
  const res = await fetchImpl(url);
  if (!res.ok) throw new Error(`Archive download failed: HTTP ${res.status}`);
  await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(partial));

  const fd = fs.openSync(partial, 'r');
  const head = Buffer.alloc(4);
  const n = fs.readSync(fd, head, 0, 4, 0);
  fs.closeSync(fd);
  const archive = path.join(dir, 'cache' + archiveExtension(head.subarray(0, n)));
  fs.renameSync(partial, archive);

  const size = fs.statSync(archive).size;
  core.info(`Downloaded ${size} bytes to ${archive}`);
  core.setOutput('archive-path', archive);
  core.setOutput('version', version);
  core.setOutput('size', String(size));
}

module.exports = run;
module.exports.pickVersion = pickVersion;
module.exports.archiveExtension = archiveExtension;
module.exports.getDownloadUrl = getDownloadUrl;
