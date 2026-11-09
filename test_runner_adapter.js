// Run with: node test_runner_adapter.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const storage = new Map();
const hooks = {};
const context = {
  __JNR_TEST__: hooks,
  localStorage: {
    getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
  },
  crypto: { randomUUID: () => 'test-runner' },
  document: { readyState: 'loading', addEventListener() {} },
};
vm.runInNewContext(fs.readFileSync('jagex_name_runner.user.js', 'utf8'), context);
const decide = hooks.detectVerdict;
assert.equal(typeof decide, 'function');

const blank = { errorAppearedAt: 0, errorChangedAt: 0, buttonChangedAt: 0,
  checkmarkAt: 0, networkAt: 0, now: 3000 };
assert.equal(decide({ error: true, buttonUsable: false }, blank), null,
  'an old red message must not mark a new candidate unavailable');
assert.equal(decide({ error: true, buttonUsable: false }, { ...blank, networkAt: 2800 }), 'unavailable',
  'a fresh completed check can confirm unchanged red text');
assert.equal(decide({ error: false, buttonUsable: true }, blank), null,
  'a previously enabled button alone is stale');
assert.equal(decide({ error: false, buttonUsable: true }, { ...blank, checkmarkAt: 2900 }), 'available');
assert.equal(decide({ error: false, buttonUsable: true }, { ...blank, buttonChangedAt: 2900 }), 'available');
assert.equal(decide({ error: true, buttonUsable: true }, { ...blank, buttonChangedAt: 2900 }), null,
  'the red unavailable message wins over a button change');
assert.equal(decide({ error: true, buttonUsable: false }, { ...blank, networkAt: 2950 }), null,
  'do not read the form before a completed check settles');
console.log('Runner adapter states passed');
