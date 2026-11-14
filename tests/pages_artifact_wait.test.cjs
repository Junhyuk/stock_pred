'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const {waitForArtifact} = require('../.github/actions/wait-for-pages-artifact/index.cjs');

const claims = {scp: 'Actions.ExampleScope Actions.Results:run-backend:job-backend'};
const token = `header.${Buffer.from(JSON.stringify(claims)).toString('base64url')}.signature`;
const env = {ACTIONS_RUNTIME_TOKEN: token, ACTIONS_RESULTS_URL: 'https://results.example.test/'};
const wanted = {name: 'github-pages', database_id: '42', size: '1234'};

function fixture(responses, options = {}) {
  let clock = 0;
  const calls = [];
  return {
    calls,
    args: {artifactId: '42', env, budgetMs: 60, pollMs: 20,
      now: () => clock, sleep: async ms => {clock += ms;},
      request: async (url, init) => {
        calls.push({url, init});
        const next = responses[Math.min(calls.length - 1, responses.length - 1)];
        if (next instanceof Error) throw next;
        if (options.requestElapsed) clock += options.requestElapsed;
        return {ok: !next.httpError, status: next.httpError || 200,
          json: async () => next.body ?? {artifacts: next.artifacts}};
      }, ...options.args},
  };
}

test('waits for the exact uploaded ID on the same Twirp list surface', async () => {
  const f = fixture([{artifacts: [{name: 'reliable-forecast-archives', database_id: '9'}]},
    {artifacts: [{...wanted, database_id: '41'}]}, {artifacts: [wanted]}]);
  const result = await waitForArtifact(f.args);
  assert.equal(result.artifactId, '42');
  assert.equal(result.attempts, 3);
  assert.equal(f.calls[0].url,
    'https://results.example.test/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts');
  assert.equal(f.calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(f.calls[0].init.body),
    {workflow_run_backend_id: 'run-backend', workflow_job_run_backend_id: 'job-backend'});
  assert.equal(f.calls[0].init.headers.Authorization, `Bearer ${token}`);
  assert.equal(f.calls[0].init.redirect, 'error');
});

test('empty protobuf response and permanent missing ID fail at the deadline', async () => {
  const f = fixture([{body: {}}]);
  await assert.rejects(waitForArtifact(f.args), /not visible within/);
  assert.equal(f.calls.length, 3);
});

test('a wrong ID with the right name never releases deployment', async () => {
  const f = fixture([{artifacts: [{...wanted, database_id: '41'}]}]);
  await assert.rejects(waitForArtifact(f.args), /not visible within/);
});

test('duplicate names fail even when one ID is the expected upload', async () => {
  const f = fixture([{artifacts: [wanted, {...wanted, database_id: '43'}]}]);
  await assert.rejects(waitForArtifact(f.args), /Multiple artifacts named github-pages/);
  assert.equal(f.calls.length, 1);
});

test('HTTP403 is explicit, immediate and does not echo the token', async () => {
  const f = fixture([{httpError: 403}]);
  await assert.rejects(waitForArtifact(f.args), error => {
    assert.match(error.message, /HTTP 403/);
    assert.ok(!error.message.includes(token));
    return true;
  });
  assert.equal(f.calls.length, 1);
});

test('HTTP request timeout fails without claiming readiness', async () => {
  const f = fixture([new DOMException('sensitive transport detail', 'TimeoutError')]);
  await assert.rejects(waitForArtifact(f.args), /request timed out/);
  assert.equal(f.calls.length, 1);
});

test('the request abort signal is bounded by the remaining overall budget', async () => {
  let signal;
  const f = fixture([], {args: {budgetMs: 20, request: async (_url, init) => {
    signal = init.signal;
    return new Promise((_resolve, reject) => {
      const keepAlive = setTimeout(() => reject(new Error('abort signal was not enforced')), 200);
      signal.addEventListener('abort', () => {clearTimeout(keepAlive); reject(signal.reason);}, {once: true});
    });
  }}});
  await assert.rejects(waitForArtifact(f.args), /request timed out/);
  assert.equal(signal.aborted, true);
});

test('protobuf lowerCamelCase databaseId also matches the uploaded ID', async () => {
  const f = fixture([{artifacts: [{name: 'github-pages', databaseId: '42'}]}]);
  assert.equal((await waitForArtifact(f.args)).artifactId, '42');
});

test('a successful response arriving beyond total budget is still too late', async () => {
  const f = fixture([{artifacts: [wanted]}], {requestElapsed: 61});
  await assert.rejects(waitForArtifact(f.args), /not visible within/);
});

test('malformed API data fails and cannot pass the deployment gate', async () => {
  const f = fixture([{body: {artifacts: 'wrong shape'}}]);
  await assert.rejects(waitForArtifact(f.args), /Invalid artifact list response/);
  const nested = fixture([{artifacts: [[]]}]);
  await assert.rejects(waitForArtifact(nested.args), /Invalid artifact list response/);
});

test('a named artifact with malformed ID fails without waiting', async () => {
  for (const database_id of [undefined, null, {}, ['42'], 0, -1, 1.5]) {
    const f = fixture([{artifacts: [{name: 'github-pages', database_id}]}]);
    await assert.rejects(waitForArtifact(f.args), /Invalid artifact ID/);
    assert.equal(f.calls.length, 1);
  }
});

test('attempt-specific name must match as well as the uploaded ID', async () => {
  const f = fixture([{artifacts: [wanted]}, {artifacts: [{...wanted, name: 'github-pages-2'}]}],
    {args: {artifactName: 'github-pages-2'}});
  assert.equal((await waitForArtifact(f.args)).attempts, 2);
});

test('missing runtime scope or invalid ID fails before any request', async () => {
  for (const args of [{artifactId: ''}, {env: {...env, ACTIONS_RUNTIME_TOKEN: ''}},
    {env: {...env, ACTIONS_RUNTIME_TOKEN: 'header.e30.signature'}}]) {
    const f = fixture([{artifacts: [wanted]}], {args});
    await assert.rejects(waitForArtifact(f.args));
    assert.equal(f.calls.length, 0);
  }
});
