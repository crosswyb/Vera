"""Downloaded updates stay in the app until the user chooses installation."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkbenchUpdateTests(unittest.TestCase):
    def test_download_progress_and_installation_follow_native_state(self):
        script = r"""
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
const nodes = new Map(), $ = id => {
  if (!nodes.has(id)) nodes.set(id, {listeners:{},
    removeAttribute(name){delete this[name];}, addEventListener(kind, action){this.listeners[kind]=action;}});
  return nodes.get(id);
};
let desktop = true;
const nativeAvailable = () => desktop;
const commands = [];
const nativePost = command => commands.push(command);
const text = (node, value) => { node.textContent = value; };
const state = {native:{updates:{state:'checking', currentVersion:'0.1.0', currentBuild:57}}};
eval(source.slice(source.indexOf('  function renderUpdates()'), source.indexOf('  function renderSettings()')) + '; globalThis.render = renderUpdates;');
render();
assert.match($('update-current-version').textContent, /0.1.0.*build 57/);
assert.equal($('check-updates').disabled, true);
assert.match($('update-status').textContent, /正在检查/);
const update = state.native.updates;
Object.assign(update, {state:'downloading', available:true, latestVersion:'v0.1.1-build59', checkedAt:1,
  downloadedBytes:5 * 1024 * 1024, totalBytes:20 * 1024 * 1024, downloadProgress:0.25,
  notes:'修复转写中断说明。<img src=x onerror=alert(1)>'});
render();
assert.equal($('update-available').hidden, false);
assert.match($('update-available').textContent, /正在下载更新.*25%/);
assert.equal($('check-updates').disabled, true);
assert.equal($('install-update').hidden, true);
assert.equal($('update-download').hidden, false);
assert.equal($('update-download-progress').value, 0.25);
assert.match($('update-download-detail').textContent, /25%.*5.0 \/ 20.0 MB/);
assert.match($('update-status').textContent, /自动下载.*完成后.*安装/);
assert.match($('update-notes').textContent, /<img src=x onerror=alert\(1\)>/);
// Unknown length is indeterminate rather than a misleading zero percent.
delete update.downloadProgress;
update.totalBytes = 0;
render();
assert.equal($('update-download-progress').value, undefined);
assert.doesNotMatch($('update-download-detail').textContent, /%/);
Object.assign(update, {state:'verifying'});
render();
assert.equal($('update-download').hidden, true);
assert.equal($('check-updates').disabled, true);
assert.equal($('install-update').hidden, true);
assert.match($('update-status').textContent, /已下载.*正在验证/);
Object.assign(update, {state:'ready', installBlockedReason:'正在录音，请在收听结束后安装。'});
render();
assert.equal($('update-download').hidden, true);
assert.equal($('install-update').hidden, false);
assert.equal($('install-update').disabled, true);
assert.equal($('update-install-blocked').hidden, false);
assert.match($('update-install-blocked').textContent, /正在录音/);
assert.match($('update-available').textContent, /已下载.*安装/);
eval(source.slice(source.indexOf("  $('check-updates').addEventListener"), source.indexOf("  $('speech-gap-add-text').addEventListener")));
$('install-update').listeners.click();
assert.deepEqual(commands, []);
update.installBlockedReason = '';
render();
assert.equal($('install-update').disabled, false);
assert.equal($('update-install-blocked').hidden, true);
assert.equal($('install-update').textContent, '安装并重新打开');
assert.match($('update-status').textContent, /已下载/);
// The desktop handles the confirmation; the page sends only the install action.
$('install-update').listeners.click();
assert.deepEqual(commands, ['installUpdate']);
Object.assign(update, {state:'installing'});
render();
assert.equal($('check-updates').disabled, true);
assert.equal($('install-update').disabled, true);
assert.match($('update-status').textContent, /正在安装.*重新打开/);
// A failed download retains the notice and notes and offers an in-app retry.
Object.assign(update, {state:'error', failureStage:'download', error:'HTTP 403'});
render();
assert.equal($('update-available').hidden, false);
assert.match($('update-status').textContent, /下载更新失败.*403/);
assert.equal($('check-updates').disabled, false);
assert.equal($('check-updates').textContent, '重试下载');
assert.equal($('install-update').hidden, true);
$('check-updates').listeners.click();
assert.deepEqual(commands, ['installUpdate', 'checkForUpdates']);
Object.assign(update, {failureStage:'validation', error:'更新包包含无效路径'});
render();
assert.match($('update-status').textContent, /更新包验证失败.*无效路径/);
assert.equal($('check-updates').textContent, '检查更新');
assert.equal($('open-update-release').hidden, false);
$('open-update-release').listeners.click();
assert.equal(commands.at(-1), 'openUpdateRelease');
Object.assign(update, {state:'current', available:false, latestVersion:'v0.1.1-build58', error:''});
render();
assert.equal($('open-update-release').hidden, true);
assert.match($('update-status').textContent, /当前没有更新/);
assert.equal($('update-release-notes').hidden, true);
assert.equal($('update-available').hidden, true);
Object.assign(update, {state:'error', failureStage:'check', error:'请求超时'});
render();
assert.match($('update-status').textContent, /未能检查更新.*请求超时/);
assert.equal($('check-updates').disabled, false);
desktop = false;
Object.assign(update, {state:'ready', available:true});
render();
assert.match($('update-status').textContent, /请在桌面 App/);
assert.equal($('check-updates').disabled, true);
assert.equal($('update-available').hidden, true);
assert.equal($('install-update').hidden, true);
assert.equal($('install-update').disabled, true);
assert.equal($('update-download').hidden, true);
"""
        subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')], check=True)

    def test_update_settings_offer_installation_and_a_recovery_download(self):
        template = (ROOT / 'templates/workbench.html').read_text()
        source = (ROOT / 'static/workbench.js').read_text()
        self.assertIn('id="install-update"', template)
        self.assertIn('发现新版会自动下载', template)
        self.assertIn('id="open-update-release" type="button" hidden', template)
        self.assertIn("nativePost('openUpdateRelease')", source)


if __name__ == '__main__':
    unittest.main()
