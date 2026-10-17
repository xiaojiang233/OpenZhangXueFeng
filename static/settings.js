import { request } from './transport.js';
import { toast } from './renderer.js';

const secretNames = ['deepseek_api_key', 'fish_audio_api_key', 'langsearch_api_key'];
const publicNames = [
  'deepseek_base_url',
  'deepseek_model',
  'fish_audio_voice_id',
  'fish_audio_latency',
];

export function initApiSettings({ onSaved, closeSidebar }) {
  const dialog = document.getElementById('apiSettingsDialog');
  const form = document.getElementById('apiSettingsForm');
  const fields = document.getElementById('apiSettingsFields');
  const message = document.getElementById('apiSettingsMessage');
  const save = document.getElementById('btnApiSettingsSave');
  let epoch = 0;
  let controller = null;
  let busy = false;
  let loaded = false;
  const status = (text, error = false) => {
    message.textContent = text;
    message.classList.toggle('error', error);
  };
  const setBusy = (value) => {
    busy = value;
    fields.disabled = value || !loaded;
    save.disabled = value || !loaded;
    form.setAttribute('aria-busy', String(value));
  };
  const resetSecrets = () => {
    secretNames.forEach((name) => {
      const input = form.elements[name];
      input.value = '';
      input.type = 'password';
    });
    form.querySelectorAll('[data-reveal]').forEach((button) => {
      button.textContent = '显示';
      button.setAttribute('aria-pressed', 'false');
    });
  };
  function populate(data) {
    resetSecrets();
    publicNames.forEach((name) => {
      form.elements[name].value = data.config[name] ?? '';
    });
    secretNames.forEach((name) => {
      const configured = Boolean(data.configured[name]);
      form.elements[name].placeholder = configured ? '已配置，留空保留原密钥' : '输入 API Key';
      const clear = form.querySelector(`[data-clear="${name}"]`);
      clear.checked = false;
      clear.closest('label').hidden = !configured;
    });
    ['chat', 'voice', 'search'].forEach((service) => {
      const badge = document.getElementById(`${service}Configured`);
      badge.textContent = data.services[service] ? '已配置' : '未配置';
      badge.classList.toggle('configured', Boolean(data.services[service]));
    });
  }
  function payload() {
    const values = Object.fromEntries(
      publicNames.map((name) => [name, form.elements[name].value.trim()]),
    );
    secretNames.forEach((name) => {
      const clear = form.querySelector(`[data-clear="${name}"]`).checked;
      const value = form.elements[name].value.trim();
      if (clear || value) values[name] = clear ? '' : value;
    });
    return values;
  }
  async function open() {
    if (dialog.open) return;
    closeSidebar();
    loaded = false;
    controller = new AbortController();
    const current = ++epoch;
    form.reset();
    resetSecrets();
    form.querySelectorAll('[data-test-result]').forEach((node) => {
      node.textContent = '';
      node.className = 'settings-test-result';
    });
    status('正在读取配置…');
    setBusy(true);
    dialog.showModal();
    document.getElementById('btnApiSettingsClose').focus();
    try {
      const data = await request('/api/settings', { signal: controller.signal });
      if (current !== epoch) return;
      populate(data);
      loaded = true;
      status(
        data.load_error
          ? '上次保存的配置无法读取，请检查后重新保存。'
          : '已有密钥不会显示；修改后点击“保存并应用”。',
        data.load_error,
      );
    } catch (error) {
      if (current === epoch && error.name !== 'AbortError')
        status(error.message || '无法读取配置，请稍后重试。', true);
    } finally {
      if (current === epoch) setBusy(false);
    }
  }
  const close = () => {
    if (!busy || !loaded) dialog.close();
  };
  dialog.addEventListener('close', () => {
    epoch++;
    controller?.abort();
    controller = null;
    resetSecrets();
    form.reset();
    loaded = false;
    setBusy(false);
  });
  dialog.addEventListener('cancel', (event) => {
    if (busy && loaded) event.preventDefault();
  });
  ['btnApiSettings', 'btnConfigureApi'].forEach((id) =>
    document.getElementById(id).addEventListener('click', open),
  );
  ['btnApiSettingsClose', 'btnApiSettingsCancel'].forEach((id) =>
    document.getElementById(id).addEventListener('click', close),
  );
  form.querySelectorAll('[data-reveal]').forEach((button) =>
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.reveal);
      const visible = input.type === 'password';
      input.type = visible ? 'text' : 'password';
      button.textContent = visible ? '隐藏' : '显示';
      button.setAttribute('aria-pressed', String(visible));
    }),
  );
  secretNames.forEach((name) => {
    const input = form.elements[name];
    const clear = form.querySelector(`[data-clear="${name}"]`);
    clear.addEventListener('change', () => {
      if (clear.checked) input.value = '';
    });
    input.addEventListener('input', () => {
      if (input.value) clear.checked = false;
    });
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !loaded || !form.reportValidity()) return;
    const current = epoch;
    setBusy(true);
    status('正在保存并应用…');
    save.textContent = '保存中…';
    try {
      const data = await request('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload()),
        signal: controller.signal,
      });
      if (current !== epoch) return;
      populate(data);
      status(data.message);
      await onSaved();
      toast('API 配置已保存并立即生效');
    } catch (error) {
      if (current === epoch) status(error.message || '保存失败，请重试。', true);
    } finally {
      if (current === epoch) {
        setBusy(false);
        save.textContent = '保存并应用';
      }
    }
  });
  form.querySelectorAll('[data-test-service]').forEach((button) =>
    button.addEventListener('click', async () => {
      if (busy || !loaded) return;
      const service = button.dataset.testService;
      const section = button.closest('.settings-service');
      for (const input of section.querySelectorAll('input,select')) {
        if (!input.reportValidity()) return;
      }
      const current = epoch;
      const result = form.querySelector(`[data-test-result="${service}"]`);
      const values = payload();
      const relevant =
        service === 'chat'
          ? ['deepseek_api_key', 'deepseek_base_url', 'deepseek_model']
          : service === 'voice'
            ? ['fish_audio_api_key', 'fish_audio_voice_id', 'fish_audio_latency']
            : ['langsearch_api_key'];
      const body = {
        service,
        ...Object.fromEntries(
          relevant
            .filter((name) => Object.hasOwn(values, name))
            .map((name) => [name, values[name]]),
        ),
      };
      setBusy(true);
      result.textContent = '正在测试…';
      result.className = 'settings-test-result';
      try {
        const data = await request('/api/settings/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        if (current !== epoch) return;
        result.textContent = data.message;
        result.classList.toggle('error', !data.ok);
        result.classList.toggle('success', data.ok);
      } catch (error) {
        if (current === epoch) {
          result.textContent = error.message || '测试失败，请重试。';
          result.classList.add('error');
        }
      } finally {
        if (current === epoch) setBusy(false);
      }
    }),
  );
}
