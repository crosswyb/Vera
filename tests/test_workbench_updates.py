"""Visible update states use the desktop's shared result without losing notices."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkbenchUpdateTests(unittest.TestCase):
    def test_update_controls_follow_native_results_and_render_release_notes_as_text(self):
        script = r"""
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
const nodes = new Map(), $ = id => {
  if (!nodes.has(id)) nodes.set(id, {});
  return nodes.get(id);
};
let desktop = true;
const nativeAvailable = () => desktop;
const text = (node, value) => { node.textContent = value; };
const state = {native:{updates:{state:'checking', currentVersion:'0.1.0', currentBuild:57}}};
eval(source.slice(source.indexOf('  function renderUpdates()'), source.indexOf('  function renderSettings()')) + '; globalThis.render = renderUpdates;');
render();
assert.match($('update-current-version').textContent, /0.1.0.*build 57/);
assert.equal($('check-updates').disabled, true);
assert.match($('update-status').textContent, /正在检查/);
const update = state.native.updates;
Object.assign(update, {state:'available', available:true, latestVersion:'v0.1.0-build58', checkedAt:1,
  notes:'修复转写中断说明。<img src=x onerror=alert(1)>'});
render();
assert.equal($('update-available').hidden, false);
assert.equal($('check-updates').disabled, false);
assert.equal($('open-update-release').hidden, false);
assert.match($('update-notes').textContent, /<img src=x onerror=alert\(1\)>/);
// A failed refresh must preserve a previously found update and its notes.
Object.assign(update, {state:'error', error:'HTTP 403'});
render();
assert.equal($('update-available').hidden, false);
assert.match($('update-status').textContent, /build58.*403/);
Object.assign(update, {state:'current', available:false, latestVersion:'v0.1.0-build57', error:''});
render();
assert.match($('update-status').textContent, /当前没有更新/);
assert.equal($('update-release-notes').hidden, true);
assert.equal($('update-available').hidden, true);
Object.assign(update, {state:'error', error:'请求超时'});
render();
assert.match($('update-status').textContent, /未能检查更新.*请求超时/);
assert.equal($('check-updates').disabled, false);
assert.equal($('open-update-release').hidden, false);
desktop = false;
render();
assert.match($('update-status').textContent, /请在桌面 App/);
assert.equal($('check-updates').disabled, true);
assert.equal($('update-available').hidden, true);
"""
        subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')], check=True)


if __name__ == '__main__':
    unittest.main()
