/* Speech settings drafts, verification results, and meeting upload consent. */
'use strict';
window.createSpeechSettings = function ({api, state, $, text, nativeAvailable, postNative, renderRuntime, time, audioTime}) {
  const labels = {apple_local: 'Apple 中文识别', dashscope: '阿里云百炼', deepseek: 'DeepSeek 官方 ASR'};
  const sourceLabels = {system: '电脑声音', microphone: '麦克风', file: '选定的音频文件'};
  const kinds = ['live', 'file'];
  const kindLabels = {live: '实时转写', file: '文件转写'};
  const gapReasons = {
    speech_input_overflow:'待转写的音频积压，识别已中断', speech_output_overflow:'转写结果积压，识别已中断',
    speech_model_unavailable:'语音识别模型不可用或启动失败', speech_recognition_failed:'语音识别返回错误',
    speech_finalization_failed:'语音识别无法保存最终文字', speech_finalization_incomplete:'末尾转写未完整保存',
    speech_finalization_interrupted:'停止时仍有音频未完成转写', speech_inactive:'语音识别意外停止',
    speech_input_failed:'无法向识别服务传入录音', speech_replay_window:'重连或切换服务时，部分音频超出可重放范围',
    speech_timeout:'语音服务响应超时', speech_authentication_failed:'语音服务鉴权失败',
    speech_quota_exhausted:'语音服务额度不足', speech_rate_limited:'语音服务请求受限',
    speech_service_unavailable:'语音服务暂不可用', speech_authorization_ended:'音频上传授权已撤回或到期',
    delivery_backlog:'转写记录积压，部分内容未能保存', delivery_incomplete:'部分转写未确认保存',
    connection:'语音服务连接中断',
  };
  const scopeSignatures = new Map();
  let defaults = null;
  let drafts = {};
  let operation = null;
  let consent = null;
  const revocations = new Map();
  let runtimeSettings = null;
  let gapSignature = '';

  function feedback(id, message, status = '') {
    text($(id), message);
    $(id).dataset.state = status;
  }

  function label(provider) { return labels[provider] || ''; }

  function activeProvider(kind) {
    const capKey = kind === 'live' ? 'speech_provider' : 'speech_file_provider';
    const nativeKey = kind === 'live' ? 'speechLiveProvider' : 'speechFileProvider';
    return latestSettings()?.speech?.[`${kind}_provider`] || state.home?.capabilities?.[capKey] || state.native?.[nativeKey] || 'apple_local';
  }

  function savedConfig(provider) { return state.serviceSettings?.speech?.providers?.[provider] || {}; }
  function selectedProvider() { return $('speech-cloud-provider').value || 'dashscope'; }

  function createDraft(provider) {
    const saved = savedConfig(provider);
    return {
      fields: {
        ...(provider === 'dashscope' ? {base_url: saved.base_url || '', live_model: saved.live_model || '', file_model: saved.file_model || ''} : {}),
        api_key: '', clear_api_key: false,
      },
      results: {},
    };
  }

  function draftFor(provider) {
    return drafts[provider] ||= createDraft(provider);
  }

  function draftPatch(provider) {
    const draft = draftFor(provider).fields;
    return Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, typeof value === 'string' ? value.trim() : value]));
  }

  function canonicalDashscopeBase(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';
    try {
      const url = new URL(raw);
      return `https://${url.hostname}`;
    } catch (_) {
      return raw.replace(/\/+$/, '');
    }
  }

  function matchingSavedDestination(provider, patch) {
    if (provider === 'deepseek') return true;
    return canonicalDashscopeBase(patch.base_url) === canonicalDashscopeBase(savedConfig(provider).base_url);
  }

  function editedKinds(provider, patch) {
    const saved = savedConfig(provider);
    if (patch.api_key || patch.clear_api_key || !matchingSavedDestination(provider, patch)) return kinds;
    if (provider === 'deepseek') return [];
    return kinds.filter(kind => patch[`${kind}_model`] !== saved[`${kind}_model`]);
  }

  function resultFor(provider, kind) {
    const draft = draftFor(provider);
    if (draft.results[kind]) return draft.results[kind];
    if (editedKinds(provider, draftPatch(provider)).includes(kind)) return null;
    return state.serviceSettings?.speech?.verification?.[provider]?.[kind];
  }

  function reportFor(provider, kind) {
    const result = resultFor(provider, kind);
    if (!result || result.status === 'not_tested') return [`${kindLabels[kind]}尚未测试`, ''];
    if (['pending', 'running'].includes(result.status)) return [`正在测试${kindLabels[kind]}…`, ''];
    const passed = result.status === 'passed';
    const title = passed ? '测试通过' : `测试失败 · ${result.error || '未取得完整结果'}`;
    const details = [result.model, Number.isFinite(result.duration_ms) ? `${result.duration_ms} 毫秒` : '', result.tested_at ? time(result.tested_at, true) : ''].filter(Boolean);
    return [[title, details.join(' · '), result.text || ''].filter(Boolean).join('\n'), passed ? 'success' : 'error'];
  }

  function writeFields() {
    const draft = draftFor(selectedProvider()).fields;
    $('speech-base-url').value = draft.base_url || '';
    $('speech-live-model').value = draft.live_model || '';
    $('speech-file-model').value = draft.file_model || '';
    $('speech-key').value = draft.api_key;
  }

  function fill(settings) {
    if (defaults && settings.speech && defaults.revision === settings.revision) { render(); return; }
    defaults = settings.speech ? {revision: settings.revision, live: settings.speech.live_provider,
      file: settings.speech.file_provider, fallback: settings.speech.fallback_to_apple} : null;
    drafts = {};
    writeFields();
    render();
  }

  function reset() {
    const pending = operation;
    operation = null; defaults = null; drafts = {}; consent = null; runtimeSettings = null;
    scopeSignatures.clear(); revocations.clear(); gapSignature = '';
    $('speech-key').value = '';
    if (pending?.id && nativeAvailable()) postNative({action:'cancelSpeechTest', testId:pending.id});
  }

  function render() {
    const settings = state.serviceSettings;
    const owner = state.home?.member?.role === 'owner';
    const busy = Boolean(operation || state.settingsBusy || state.settingsRequest);
    const editable = Boolean(owner && settings?.editable && settings.speech && defaults);
    const provider = selectedProvider();
    const patch = draftPatch(provider);
    const savedKey = Boolean(savedConfig(provider).api_key_configured && matchingSavedDestination(provider, patch));
    const hasKey = Boolean(patch.api_key || savedKey && !patch.clear_api_key);
    text($('speech-active-settings'), `已启用：实时会议 ${label(settings?.speech?.live_provider || activeProvider('live'))} · 音频文件 ${label(settings?.speech?.file_provider || activeProvider('file'))}`);
    $('speech-default-fields').disabled = !editable || busy;
    $('speech-cloud-fields').disabled = !editable || busy;
    $('speech-dashscope-fields').hidden = provider !== 'dashscope';
    $('speech-deepseek-info').hidden = provider !== 'deepseek';
    $('speech-live-test-row').hidden = provider !== 'dashscope';
    $('speech-clear-key').disabled = busy || !editable || !savedKey || patch.clear_api_key;
    text($('speech-key-state'), patch.clear_api_key ? '将清除' : savedKey ? '已保存' : '未配置');
    $('speech-key').placeholder = patch.clear_api_key ? '保存后清除' : savedKey ? '留空保留密钥' : '请输入语音服务密钥';
    const readyToTest = editable && !busy && nativeAvailable() && state.native?.ready && hasKey;
    for (const kind of kinds) {
      const selected = defaults?.[kind] || 'apple_local';
      const picker = $(`speech-${kind}-provider`);
      for (const option of picker.options) {
        const value = option.value;
        const candidate = value === provider ? resultFor(value, kind) : settings?.speech?.verification?.[value]?.[kind];
        option.disabled = value !== 'apple_local' && candidate?.status !== 'passed';
      }
      picker.value = selected;
      const testButton = $(`speech-test-${kind}`);
      testButton.disabled = !readyToTest || (provider === 'dashscope' && !(patch.base_url && patch[`${kind}_model`])) || (provider === 'deepseek' && kind === 'live');
      feedback(`speech-${kind}-result`, ...reportFor(provider, kind));
    }
    $('speech-fallback').checked = defaults?.fallback ?? true;
    const choicesReady = kinds.every(kind => !$(`speech-${kind}-provider`).selectedOptions[0]?.disabled);
    const savingEdit = editedKinds(provider, patch).length > 0;
    const unsavedProviders = ['dashscope', 'deepseek'].filter(value => {
      const draft = draftFor(value);
      return editedKinds(value, draftPatch(value)).length || Object.values(draft.results).some(result => result.status === 'passed');
    });
    text($('speech-unsaved-settings'), unsavedProviders.length
      ? `${unsavedProviders.map(label).join('、')}有待保存的配置或测试结果。当前保存对象为${label(provider)}。` : '');
    const editReady = !savingEdit || !hasKey || kinds.some(kind => draftFor(provider).results[kind]?.status === 'passed' || resultFor(provider, kind)?.status === 'passed');
    $('speech-save').disabled = !editable || busy || !choicesReady || !editReady;
    $('speech-reload').disabled = !owner || busy;
    $('speech-cancel-test').hidden = !operation?.test || !operation.id;
    $('speech-cancel-test').disabled = Boolean(operation?.cancelled);
    if (!owner) feedback('speech-settings-result', '只有工作区主持人可修改转写设置。');
    else if (settings && !editable) feedback('speech-settings-result', '当前运行方式无法保存语音转写设置。');
    else if (!nativeAvailable()) feedback('speech-settings-result', '请在桌面 App 中测试云端转写。');
  }

  function readFields(event) {
    const provider = selectedProvider();
    const draft = draftFor(provider);
    const before = draftPatch(provider);
    if (provider === 'dashscope') {
      draft.fields.base_url = $('speech-base-url').value;
      draft.fields.live_model = $('speech-live-model').value;
      draft.fields.file_model = $('speech-file-model').value;
    }
    draft.fields.api_key = $('speech-key').value;
    if (event.currentTarget === $('speech-key')) draft.fields.clear_api_key = false;
    const after = draftPatch(provider);
    for (const kind of kinds) {
      if (before.base_url !== after.base_url || before.api_key !== after.api_key || before.clear_api_key !== after.clear_api_key
          || before[`${kind}_model`] !== after[`${kind}_model`]) delete draft.results[kind];
    }
    feedback('speech-settings-result', '修改尚未保存。');
    render();
  }

  async function runTest(kind) {
    if (operation || $(`speech-test-${kind}`).disabled || !$('speech-settings-form').reportValidity()) return;
    const provider = selectedProvider();
    const job = {test:true, token:state.token, provider, kind};
    const draft = draftFor(provider);
    operation = job;
    draft.results[kind] = {status:'pending'};
    feedback('speech-settings-result', '');
    render();
    const current = () => operation === job && state.token === job.token;
    const path = '/live/v2/settings/speech/tests';
    try {
      const value = await api(path, {method:'POST', body:{provider, kind, revision:state.serviceSettings.revision, config:draftPatch(provider)}});
      if (!current()) return;
      job.id = value.test_id;
      const priorNativeError = state.native?.speechTestError;
      postNative({action:'speechTest', testId:job.id});
      render();
      const timeout = Date.now() + 35000;
      while (current() && !job.cancelled && Date.now() < timeout) {
        const result = await api(`${path}/${encodeURIComponent(job.id)}`);
        if (!current() || job.cancelled) return;
        const passed = result.status === 'passed';
        if (passed || result.status === 'failed') {
          draft.results[kind] = {...result, testId: job.id, tested_at: result.tested_at || Date.now() / 1000,
            model: draft.fields[`${kind}_model`] || ''};
          if (passed) feedback('speech-settings-result', '测试通过。可在上方选择此服务，再保存并使用。', 'success');
          return;
        }
        const nativeError = state.native?.speechTestError;
        if (nativeError && nativeError !== priorNativeError) throw new Error(nativeError);
        await new Promise(resolve => setTimeout(resolve, 450));
      }
      if (!job.cancelled) throw new Error('测试超时，请重试。');
    } catch (error) {
      if (current() && !job.cancelled) {
        draft.results[kind] = {status:'failed', error:error.message};
        if (job.id) {
          postNative({action:'cancelSpeechTest', testId:job.id});
          void api(`${path}/${encodeURIComponent(job.id)}`, {method:'DELETE'}).catch(() => {});
        }
      }
    } finally {
      if (current() && !job.cancelled) { operation = null; render(); }
    }
  }

  async function cancelTest() {
    const job = operation;
    if (!job?.id || !job.test || job.cancelled) return;
    job.cancelled = true;
    postNative({action:'cancelSpeechTest', testId:job.id});
    render();
    try { await api(`/live/v2/settings/speech/tests/${encodeURIComponent(job.id)}`, {method:'DELETE'}); }
    catch (error) { if (operation === job) feedback('speech-settings-result', error.message, 'error'); }
    finally {
      if (operation === job) {
        operation = null;
        delete draftFor(job.provider).results[job.kind];
        feedback('speech-settings-result', '测试已取消。');
        render();
      }
    }
  }

  async function save(event) {
    event.preventDefault();
    if (operation || !defaults || $('speech-save').disabled || !$('speech-settings-form').reportValidity()) return;
    const provider = selectedProvider();
    const config = draftPatch(provider);
    const testIds = {};
    for (const [kind, result] of Object.entries(draftFor(provider).results)) {
      if (result.status === 'passed') testIds[kind] = result.testId;
    }
    const job = {token:state.token};
    operation = job;
    feedback('speech-settings-result', '正在保存…');
    render();
    try {
      const result = await api('/live/v2/settings/speech', {method:'PUT', body:{
        revision:state.serviceSettings.revision, provider, config, test_ids:testIds,
        live_provider:defaults.live, file_provider:defaults.file, fallback_to_apple:defaults.fallback,
      }});
      if (operation !== job || job.token !== state.token) return;
      const otherProvider = provider === 'dashscope' ? 'deepseek' : 'dashscope';
      const otherDraft = drafts[otherProvider];
      state.serviceSettings = result;
      fill(result);
      if (otherDraft) drafts[otherProvider] = otherDraft;
      if (nativeAvailable()) postNative({action:'refreshSpeechSettings'});
      renderRuntime();
      feedback('speech-settings-result', '已保存，下次开始收听或导入时生效。', 'success');
    } catch (error) {
      if (operation === job) feedback('speech-settings-result', error.message, 'error');
    } finally {
      if (operation === job) { operation = null; render(); }
    }
  }

  function latestSettings() {
    const saved = state.serviceSettings;
    return runtimeSettings?.revision > (saved?.revision ?? -1) ? runtimeSettings : saved;
  }

  function scope(kind, sources, settings = latestSettings()) {
    const provider = settings?.speech?.[`${kind}_provider`] || activeProvider(kind);
    if (provider === 'apple_local') return null;
    const destination = settings?.speech?.providers?.[provider]?.destinations?.[kind];
    return {provider, kind, sources, destination, revision:settings?.revision};
  }

  function scopeFor(kind, sources, provider, settings = latestSettings()) {
    if (!provider) return scope(kind, sources, settings);
    const speechSettings = {...(settings?.speech || {}), [`${kind}_provider`]: provider};
    return scope(kind, sources, {...settings, speech: speechSettings});
  }

  function scopeText(value) {
    return `将把${value.sources.map(source => sourceLabels[source]).join('、')}发送给${label(value.provider)}进行${kindLabels[value.kind]}。接收地址 ${value.destination}。`;
  }

  function granted(meeting, value) {
    const current = meeting?.speech_authorization;
    return current?.state === 'authorized' && current.provider === value.provider && current.kind === value.kind
      && current.destination === value.destination && value.sources.every(source => current.sources.includes(source));
  }

  function scopeSignature(value) {
    if (!value) return '';
    return JSON.stringify([value.provider, value.kind, value.destination, ...[...value.sources].sort()]);
  }

  async function refreshScope(kind, sources) {
    if (state.home?.member?.role !== 'owner') return scope(kind, sources);
    const token = state.token;
    const settings = await api('/live/v2/settings');
    if (token !== state.token) throw new Error('登录状态已变化，请重新操作。');
    if (settings.revision >= (latestSettings()?.revision ?? -1)) runtimeSettings = settings;
    renderRuntime();
    return scope(kind, sources);
  }

  async function checkScope(kind, sources, approved) {
    const current = await refreshScope(kind, sources);
    if (scopeSignature(current) !== scopeSignature(approved)) {
      throw new Error('语音转写方式或音频接收地址已变化，请确认更新后的设置再继续。');
    }
    return current;
  }

  function renderUpload(prefix, value, meeting = null) {
    const owner = state.home?.member?.role === 'owner';
    const signature = scopeSignature(value);
    const checkbox = $(`${prefix}-upload-agree`);
    if (scopeSignatures.get(prefix) !== signature) {
      checkbox.checked = false;
      scopeSignatures.set(prefix, signature);
    }
    const required = Boolean(value && !granted(meeting, value));
    $(`${prefix}-upload-field`).hidden = !required;
    checkbox.disabled = !owner || !value?.destination;
    checkbox.required = required && !checkbox.disabled;
    if (required) text($(`${prefix}-upload-scope`), value.destination ? scopeText(value)
      : owner ? '正在读取接收地址，请稍候。' : '请主持人确认本场音频上传。');
    return !required || Boolean(owner && value.destination && checkbox.checked);
  }

  function resetUpload(prefix) { scopeSignatures.delete(prefix); }

  async function authorize(meeting, value) {
    if (!value || granted(meeting, value)) return;
    if (!value.destination || !Number.isInteger(value.revision)) throw new Error('无法确认音频发送地址，请重新载入语音转写设置。');
    if (state.home?.member?.role !== 'owner') throw new Error('请主持人确认音频上传。');
    const {provider, kind, sources, revision} = value;
    const token = state.token;
    try {
      const result = await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/speech-authorization`, {method:'PUT', body:{provider, kind, sources, revision}});
      if (token !== state.token) throw new Error('登录状态已变化，请重新操作。');
      meeting.speech_authorization = result;
      revocations.delete(meeting.id);
    } catch (error) {
      if (error.status === 409) await refreshScope(kind, sources);
      throw error;
    }
  }

  function confirmLive(meeting, value, start) {
    if (!value || granted(meeting, value)) { start(); return; }
    if (state.home?.member?.role !== 'owner') throw new Error('请主持人确认本场的音频上传。');
    if (!value.destination) throw new Error('请前往语音转写设置，重新载入已保存的服务再开始。');
    consent = {meeting, value, start};
    $('speech-consent-submit').disabled = false;
    text($('speech-consent-error'), '');
    text($('speech-consent-scope'), scopeText(value));
    $('speech-consent-dialog').showModal();
  }

  async function submitConsent(event) {
    event.preventDefault();
    if (!consent || $('speech-consent-submit').disabled) return;
    const pending = consent;
    $('speech-consent-submit').disabled = true;
    try {
      const {kind, sources} = pending.value;
      const current = await refreshScope(kind, sources);
      if (consent !== pending || state.meetingId !== pending.meeting.id) return;
      if (scopeSignature(current) !== scopeSignature(pending.value)) {
        if (!current?.destination) {
          text($('speech-consent-error'), '语音转写设置已变化。请关闭此提示，再开始收听。');
          return;
        }
        pending.value = current;
        text($('speech-consent-scope'), scopeText(current));
        text($('speech-consent-error'), '音频接收范围已变化，请重新确认。');
        return;
      }
      await authorize(pending.meeting, current);
      if (consent !== pending || state.meetingId !== pending.meeting.id) return;
      $('speech-consent-dialog').close();
      pending.start();
    } catch (error) {
      if (consent === pending) text($('speech-consent-error'), error.message);
    } finally { $('speech-consent-submit').disabled = false; }
  }

  async function revoke() {
    const meeting = state.snapshot?.meeting;
    if (!meeting || revocations.get(meeting.id)?.requesting || meeting.speech_authorization?.state !== 'authorized') return;
    const pending = {requesting:true, version:meeting.speech_authorization.version, token:state.token};
    revocations.set(meeting.id, pending);
    renderMeeting(meeting, state.native?.meetingId === meeting.id);
    try {
      const result = await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/speech-authorization`, {method:'PUT', body:{revoke:true}});
      if (state.token !== pending.token || revocations.get(meeting.id) !== pending) return;
      pending.version = result.version;
      meeting.speech_authorization = result;
      const displayed = state.snapshot?.meeting;
      if (displayed?.id === meeting.id && (displayed.speech_authorization?.version ?? 0) <= result.version) {
        displayed.speech_authorization = result;
      }
      if (state.native?.meetingId === meeting.id) postNative({action:'revokeSpeech', meetingId:meeting.id});
      // Renewal is rejected after this response. Count the full remaining lease
      // window from receipt, using a monotonic clock.
      pending.deadline = performance.now() + 5000;
      setTimeout(() => {
        if (revocations.get(meeting.id) === pending) {
          revocations.delete(meeting.id);
          renderRuntime();
        }
      }, 5000);
    } catch (error) {
      if (revocations.get(meeting.id) === pending) pending.error = error.message;
    } finally { pending.requesting = false; renderRuntime(); }
  }

  function renderMeeting(meeting, bound) {
    const auth = meeting?.speech_authorization;
    const canRevoke = meeting?.state === 'active' && auth?.state === 'authorized' && state.home?.member?.role === 'owner';
    const revocation = revocations.get(meeting?.id);
    const sameRevocation = revocation?.requesting || (auth?.version ?? 0) <= revocation?.version;
    const currentRevocation = sameRevocation ? revocation : null;
    const stoppingUpload = currentRevocation && (currentRevocation.requesting || performance.now() < currentRevocation.deadline);
    const job = meeting?.audio_import;
    const fileMeeting = meeting?.input_kind === 'audio_file' || Boolean(job);
    const bySource = bound && !fileMeeting ? {...meeting.transcription, ...state.native?.transcription} : meeting?.transcription || {};
    const statusLabels = {connecting:'正在连接', starting:'正在连接', running:'转写中', ready:'已连接', recognizing:'转写中',
      reconnecting:'正在重新连接', local:'正在本机转写', degraded:'已切回本机', paused:'转写已暂停', error:'转写异常',
      revoked:'音频上传已停止', unavailable:'转写暂不可用', stopped:'转写已停止'};
    const statuses = Object.entries(bySource).flatMap(([source, value]) => {
      const provider = label(value.provider);
      const status = value.state || value.status;
      const details = bound && ['paused','error','unavailable'].includes(status)
        ? [gapReasons[value.code], value.detail].filter(Boolean).join(' · ') : '';
      return provider ? [[sourceLabels[source] || '转写', provider, bound ? statusLabels[status] : '', details].filter(Boolean).join(' · ')] : [];
    });
    const active = stoppingUpload ? '正在停止音频上传…' : currentRevocation?.error ? `撤回失败。${currentRevocation.error}` :
      auth?.state === 'revoked' && meeting?.state === 'active' ? '本场音频上传授权已撤回。' : '';
    if (!statuses.length && job?.provider) statuses.push(`文件转写 · ${label(job.provider)}`);
    if (!statuses.length && auth?.state === 'authorized' && meeting?.state === 'active') statuses.push(`已授权 ${label(auth.provider)} 转写${auth.sources.map(source => sourceLabels[source]).join('、')}`);
    text($('speech-meeting-state'), [active, ...statuses].filter(Boolean).join(' ｜ '));
    $('speech-meeting-status').hidden = !active && !statuses.length && !canRevoke;
    $('revoke-speech').hidden = !canRevoke || stoppingUpload;
    const captureGaps = Object.entries(meeting?.capture_epochs || {}).flatMap(([epoch, value]) =>
      (value.gaps || []).map(gap => ({...gap, capture_epoch:epoch})));
    const importedGaps = job ? (meeting?.transcription?.file?.gaps || []).map(gap => ({...gap, capture_epoch:job.import_id})) : [];
    const gaps = state.snapshot?.meeting?.id === meeting?.id && Array.isArray(state.snapshot?.speech_gaps)
      ? state.snapshot.speech_gaps : [...captureGaps, ...importedGaps];
    $('speech-gaps').hidden = gaps.length === 0;
    text($('speech-gap-count'), gaps.length ? `${gaps.length} 处转写缺失或中断 · 查看原因与影响时段` : '');
    const recordings = meeting?.recordings || [];
    const desktop = Boolean(nativeAvailable?.());
    const signature = JSON.stringify([meeting?.id, gaps, desktop, recordings.map(r => [r.id, r.epoch, r.source, r.state])]);
    if (signature !== gapSignature) {
      gapSignature = signature;
      $('speech-gap-items').replaceChildren(...gaps.map(gap => {
        const item = document.createElement('li');
        const hasRange = Number.isFinite(gap.start_ms) && Number.isFinite(gap.end_ms);
        const range = hasRange ? `${audioTime(gap.start_ms)}–${audioTime(gap.end_ms)}` : '影响时段未确认';
        const heading = document.createElement('p');
        text(heading, `${sourceLabels[gap.source] || '音频'} ${range} · 此时段未获得完整转写`);
        const reason = document.createElement('p');
        const causes = gap.causes?.length ? gap.causes : [gap];
        text(reason, causes.map(cause => [label(cause.provider), cause.detail || gapReasons[cause.code] || '未记录具体故障原因',
          cause.code ? `（${cause.code}）` : ''].filter(Boolean).join(' · ')).join(' → '));
        const play = document.createElement('button');
        play.type = 'button'; play.className = 'text-button';
        text(play, '打开原录音');
        const recording = recordings.find(r => r.source === gap.source && r.epoch === gap.capture_epoch);
        play.disabled = !desktop || !recording || !['saved','partial'].includes(recording.state);
        if (!desktop) play.title = '请在桌面 App 中打开录音';
        else if (play.disabled) play.title = '此段录音尚未保存或不可用';
        else play.title = `打开录音后查看 ${range}`;
        play.addEventListener('click', () => {
          if (!play.disabled) postNative({action:'openMeetingAudio', meetingId:meeting.id, fileId:recording.id});
        });
        item.append(heading, reason, play);
        return item;
      }));
    }
    $('speech-gap-add-text').hidden = !meeting || meeting.state !== 'active' || $('transcript-input').disabled;
  }

  $('speech-cloud-provider').addEventListener('change', () => { writeFields(); render(); });
  for (const id of ['speech-base-url', 'speech-live-model', 'speech-file-model', 'speech-key']) $(id).addEventListener('input', readFields);
  $('speech-clear-key').addEventListener('click', () => {
    const draft = draftFor(selectedProvider());
    draft.fields.api_key = ''; draft.fields.clear_api_key = true; draft.results = {};
    writeFields(); render();
  });
  for (const kind of kinds) {
    $(`speech-${kind}-provider`).addEventListener('change', event => { if (defaults) defaults[kind] = event.target.value; render(); });
    $(`speech-test-${kind}`).addEventListener('click', () => runTest(kind));
  }
  $('speech-fallback').addEventListener('change', event => { if (defaults) defaults.fallback = event.target.checked; });
  $('speech-cancel-test').addEventListener('click', cancelTest);
  $('speech-settings-form').addEventListener('submit', save);
  $('speech-consent-form').addEventListener('submit', submitConsent);
  $('speech-consent-dialog').addEventListener('close', () => { consent = null; });
  $('revoke-speech').addEventListener('click', revoke);
  return {label, activeProvider, scope, scopeFor, refreshScope, checkScope, renderUpload, resetUpload, authorize, confirmLive, renderMeeting, fill, reset, render};
};
