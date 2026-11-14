'use strict';

// Match deploy-pages/v4's DefaultArtifactClient.listArtifacts() surface, not
// the separate REST artifact index. Contract: actions/toolkit artifact package,
// internal/find/list-artifacts.ts and internal/shared/util.ts.
const {performance} = require('node:perf_hooks');

function runtimeRequest(env) {
  const token = env.ACTIONS_RUNTIME_TOKEN;
  let ids;
  try {
    const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'));
    const scope = claims.scp.split(' ').find(value => value.startsWith('Actions.Results:'));
    ids = scope.split(':');
    if (ids.length !== 3 || !ids[1] || !ids[2]) throw new Error();
  } catch {
    throw new Error('Pages artifact runtime token scope unavailable');
  }
  let url;
  try {
    url = new URL('/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts', env.ACTIONS_RESULTS_URL);
    if (url.protocol !== 'https:' || url.username || url.password) throw new Error();
  } catch {
    throw new Error('Pages artifact results service URL unavailable');
  }
  return {url: url.href, init: {method: 'POST', redirect: 'error',
    headers: {'Content-Type': 'application/json', Authorization: `Bearer ${token}`},
    body: JSON.stringify({workflow_run_backend_id: ids[1], workflow_job_run_backend_id: ids[2]})}};
}

function transportError(error) {
  return new Error(['TimeoutError', 'AbortError'].includes(error?.name)
    ? 'Pages artifact list request timed out' : 'Pages artifact list request failed');
}

async function waitForArtifact({artifactId, artifactName = 'github-pages', env = process.env, request = fetch,
  now = () => performance.now(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  budgetMs = 60000, pollMs = 2000}) {
  if (!/^[1-9]\d*$/.test(String(artifactId ?? ''))) throw new Error('Expected Pages artifact ID is required');
  if (typeof artifactName !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(artifactName)) {
    throw new Error('Invalid Pages artifact name');
  }
  if (!(budgetMs > 0 && budgetMs <= 60000 && pollMs > 0)) throw new Error('Invalid Pages artifact wait budget');
  artifactId = String(artifactId);
  const deadline = now() + budgetMs;
  const rpc = runtimeRequest(env);
  const expired = () => new Error(`Pages artifact ${artifactId} not visible within ${budgetMs}ms`);
  for (let attempts = 1; ; attempts++) {
    const remaining = deadline - now();
    if (remaining <= 0) throw expired();
    let response;
    try {
      response = await request(rpc.url, {...rpc.init,
        signal: AbortSignal.timeout(Math.max(1, Math.ceil(Math.min(10000, remaining))))});
    } catch (error) {
      throw transportError(error);
    }
    if (!response.ok) throw new Error(`Pages artifact list HTTP ${response.status}`);
    let data;
    try {
      data = await response.json();
    } catch (error) {
      if (['TimeoutError', 'AbortError'].includes(error?.name)) throw transportError(error);
      throw new Error('Invalid artifact list response');
    }
    if (now() >= deadline) throw expired();
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Invalid artifact list response');
    const artifacts = data.artifacts === undefined ? [] : data.artifacts;
    if (!Array.isArray(artifacts) || artifacts.some(item => !item || typeof item !== 'object' || Array.isArray(item))) {
      throw new Error('Invalid artifact list response');
    }
    const named = artifacts.filter(item => item.name === artifactName);
    if (named.length > 1) throw new Error(`Multiple artifacts named ${artifactName}`);
    if (named.length === 1) {
      const id = named[0].database_id ?? named[0].databaseId;
      if (!['string', 'number'].includes(typeof id) || !/^[1-9]\d*$/.test(String(id))
          || (typeof id === 'number' && !Number.isSafeInteger(id))) throw new Error('Invalid artifact ID');
      if (String(id) === artifactId) return {artifactId, attempts};
    }
    await sleep(Math.min(pollMs, Math.max(0, deadline - now())));
  }
}

module.exports = {waitForArtifact};

if (require.main === module) {
  waitForArtifact({artifactId: process.env.INPUT_ARTIFACT_ID,
    artifactName: process.env.INPUT_ARTIFACT_NAME || 'github-pages'})
    .then(result => console.log(`Pages artifact ${result.artifactId} visible after ${result.attempts} list checks.`))
    .catch(error => {console.error(`::error::${error.message}`); process.exitCode = 1;});
}
