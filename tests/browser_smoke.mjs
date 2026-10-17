/** Integration checks against disposable fake-provider app; never paid services.
 * npm install && npm run test:browser
 * Optional: TEST_PYTHON, BROWSER_EXECUTABLE_PATH, TEST_BASE_URL,
 * PLAYWRIGHT_MODULE_PATH (for preinstalled local tooling).
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = fileURLToPath(new URL('..', new URL('.', import.meta.url)));
const artifacts = path.join(root, '.tmp', 'browser-artifacts');
mkdirSync(artifacts, { recursive: true });
const base = process.env.TEST_BASE_URL || 'http://127.0.0.1:8765';
const python =
  process.env.TEST_PYTHON ||
  path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
let server;
let browser;
let logs = '';
const results = [];
const passed = (name) => {
  results.push(name);
  console.log(`PASS ${name}`);
};

try {
  if (!process.env.TEST_BASE_URL) {
    server = spawn(
      existsSync(python) ? python : 'python',
      ['-m', 'uvicorn', 'tests.browser_fixture:app', '--host', '127.0.0.1', '--port', '8765'],
      { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] },
    );
    server.stdout.on('data', (chunk) => {
      logs += chunk;
    });
    server.stderr.on('data', (chunk) => {
      logs += chunk;
    });
    server.on('error', (error) => {
      logs += error.message;
    });
  }
  const deadline = Date.now() + 20000;
  while (true) {
    try {
      if ((await fetch(`${base}/api/health`)).ok) break;
    } catch {}
    if (Date.now() > deadline) throw new Error(`Test app did not start. ${logs}`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  browser = await chromium.launch({
    headless: true,
    ...(process.env.BROWSER_EXECUTABLE_PATH
      ? { executablePath: process.env.BROWSER_EXECUTABLE_PATH }
      : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: 'reduce',
  });
  await context.addInitScript(() => {
    const NativeSocket = window.WebSocket;
    window.__qaSockets = [];
    window.WebSocket = class extends NativeSocket {
      constructor(...args) {
        super(...args);
        window.__qaSockets.push(this);
      }
    };
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(base);
  await page.waitForFunction(() => window.__qaSockets?.at(-1)?.readyState === 1);
  await page.locator('#chatEmpty').waitFor({ state: 'visible' });
  assert.equal(await page.locator('.prompt-card').count(), 4);
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2),
    true,
  );
  await page.screenshot({ path: path.join(artifacts, 'desktop-welcome.png'), fullPage: true });
  passed('desktop welcome, modules and layout');

  const security = await page.evaluate(async () => {
    const { renderMarkdown, safeUrl } = await import('/static/markdown.js');
    const host = document.createElement('div');
    host.append(
      renderMarkdown(
        '<img src=x onerror="window.__unsafe=true">\n\n[unsafe](javascript:alert(1)) [data](data:text/html,hi) **bold** `code`',
      ),
    );
    const sources = [1, 2, 3].map((n) => ({
      title: `source ${n}`,
      url: `https://example.com/${n}`,
    }));
    const ranges = document.createElement('div');
    ranges.append(renderMarkdown('[1-3] [1, 2] [999999999] [1-999999999]', sources));
    return {
      unsafeNodes: host.querySelectorAll('img,script,[onerror],[onclick]').length,
      unsafeLinks: [...host.querySelectorAll('a')].filter(
        (a) => !['https:', 'http:'].includes(a.protocol),
      ).length,
      bold: host.querySelector('strong')?.textContent,
      code: host.querySelector('code')?.textContent,
      rangeCount: ranges.querySelectorAll('.cite-badge').length,
      blocked: safeUrl('javascript:alert(1)') === null && safeUrl('data:text/html,hello') === null,
    };
  });
  assert.deepEqual(security, {
    unsafeNodes: 0,
    unsafeLinks: 0,
    bold: 'bold',
    code: 'code',
    rangeCount: 8,
    blocked: true,
  });
  passed('safe Markdown, links and bounded inclusive citations');

  const audio = await page.evaluate(async () => {
    const { encodeWAV, WaveRecorder } = await import('/static/audio.js');
    const wav = new DataView(await encodeWAV(new Float32Array([-1, 0, 1]), 48000).arrayBuffer());
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    let resolvePermission;
    navigator.mediaDevices.getUserMedia = () =>
      new Promise((resolve) => {
        resolvePermission = resolve;
      });
    let stopped = 0;
    const recorder = new WaveRecorder();
    const pending = recorder.start();
    recorder.stop(true);
    resolvePermission({ getTracks: () => [{ stop: () => stopped++ }] });
    const started = await pending;
    navigator.mediaDevices.getUserMedia = original;
    return {
      rate: wav.getUint32(24, true),
      bytes: wav.getUint32(40, true),
      low: wav.getInt16(44, true),
      high: wav.getInt16(48, true),
      started,
      stopped,
    };
  });
  assert.deepEqual(audio, {
    rate: 48000,
    bytes: 6,
    low: -32768,
    high: 32767,
    started: false,
    stopped: 1,
  });
  passed('WAV format and microphone permission cancellation cleanup');

  await page.locator('.prompt-card').first().click();
  assert.ok((await page.locator('#messageInput').inputValue()).length > 10);
  await page.locator('#btnSend').click();
  await page.locator('#messages .assistant').first().waitFor();
  await page.locator('#btnStop').waitFor({ state: 'hidden' });
  assert.equal(await page.locator('#messages .user').count(), 1);
  assert.equal(await page.locator('#messages .assistant').count(), 1);
  assert.ok((await page.locator('#messages').innerText()).includes('选择思路'));
  assert.equal(
    await page.locator('.cite-badge').first().getAttribute('href'),
    'https://example.com/admissions',
  );
  await page.screenshot({ path: path.join(artifacts, 'desktop-chat.png'), fullPage: true });
  passed('prompt, streamed answer, history and source links');

  await page.evaluate(() => window.__qaSockets.at(-1).close());
  await page.waitForFunction(
    () => window.__qaSockets.length >= 2 && window.__qaSockets.at(-1).readyState === 1,
  );
  await page.locator('#messages .assistant .copy-button').waitFor();
  assert.equal(await page.locator('#messages .user').count(), 1);
  passed('reconnection restores conversation without duplicate sends');

  await page.locator('#btnNewChat').click();
  await page.locator('#chatEmpty').waitFor({ state: 'visible' });
  await page.locator('#messageInput').fill('测试慢回复');
  await page.locator('#btnSend').click();
  await page.locator('#messages .assistant').filter({ hasText: '正在梳理你的问题' }).waitFor();
  await page.locator('#btnStop').click();
  await page.locator('#btnStop').waitFor({ state: 'hidden' });
  assert.ok((await page.locator('#messages').innerText()).includes('正在梳理你的问题'));
  assert.ok(!(await page.locator('#messages').innerText()).includes('迟到的结果'));
  passed('stop interrupts generation while preserving partial text');

  await page.locator('#historySearch').fill('no-matching-history-123');
  assert.equal(await page.locator('.conv-open').count(), 0);
  await page.locator('#historySearch').fill('');
  assert.ok((await page.locator('.conv-open').count()) >= 1);
  await page.locator('#btnThemeToggle').click();
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  await page.reload();
  await page.waitForFunction(() => window.__qaSockets?.at(-1)?.readyState === 1);
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  await page.locator('#btnThemeToggle').click();
  await page.locator('#btnHelp').click();
  await page.locator('#helpDialog').waitFor({ state: 'visible' });
  await page.keyboard.press('Escape');
  passed('history search, saved theme and accessible help dialog');

  await page.locator('.conv-delete').first().click();
  await page.locator('#deleteDialog').waitFor({ state: 'visible' });
  const previousCount = await page.locator('.conv-open').count();
  await page.locator('#btnDeleteConfirm').click();
  await page.waitForFunction(
    (count) => document.querySelectorAll('.conv-open').length < count,
    previousCount,
  );
  passed('conversation deletion and confirmation');

  const mobile = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    reducedMotion: 'reduce',
  });
  const phone = await mobile.newPage();
  phone.on('pageerror', (error) => errors.push(error.message));
  await phone.goto(base);
  await phone.locator('#chatEmpty').waitFor({ state: 'visible' });
  assert.equal(
    await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2),
    true,
  );
  await phone.screenshot({ path: path.join(artifacts, 'mobile-welcome.png'), fullPage: true });
  await phone.locator('#btnMenu').click();
  assert.equal(await phone.locator('#btnMenu').getAttribute('aria-expanded'), 'true');
  await phone.locator('#sidebarScrim').click({ position: { x: 340, y: 200 } });
  assert.equal(await phone.locator('#btnMenu').getAttribute('aria-expanded'), 'false');
  passed('mobile layout and dismissible navigation');

  await page.locator('#btnApiSettings').click();
  await page.locator('#apiSettingsFields').waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.getElementById('apiSettingsFields').disabled);
  assert.equal(await page.locator('#deepseekApiKey').inputValue(), '');
  assert.ok((await page.locator('#deepseekApiKey').getAttribute('placeholder')).includes('已配置'));
  await page.locator('#deepseekApiKey').fill('ui-private-model-key');
  await page.locator('#deepseekModel').fill('ui-configured-model');
  await page.locator('[data-reveal="deepseekApiKey"]').click();
  assert.equal(await page.locator('#deepseekApiKey').getAttribute('type'), 'text');
  await page.locator('[data-reveal="deepseekApiKey"]').click();
  await page.locator('[data-test-service="chat"]').click();
  await page.locator('[data-test-result="chat"]').filter({ hasText: '通过' }).waitFor();
  const beforeSave = await page.evaluate(() => fetch('/api/settings').then((r) => r.json()));
  assert.notEqual(beforeSave.config.deepseek_model, 'ui-configured-model');
  await page.locator('#btnApiSettingsSave').click();
  await page.locator('#apiSettingsMessage').filter({ hasText: '立即生效' }).waitFor();
  assert.equal(await page.locator('#deepseekApiKey').inputValue(), '');
  const afterSave = await page.evaluate(() => fetch('/api/settings').then((r) => r.json()));
  assert.equal(afterSave.config.deepseek_model, 'ui-configured-model');
  assert.equal(afterSave.configured.deepseek_api_key, true);
  assert.ok(!JSON.stringify(afterSave).includes('ui-private-model-key'));
  assert.ok(
    !(await page.evaluate(() => JSON.stringify(localStorage))).includes('ui-private-model-key'),
  );
  await page.screenshot({ path: path.join(artifacts, 'desktop-api-settings.png'), fullPage: true });
  await page.locator('#btnApiSettingsClose').click();
  await page.locator('#btnApiSettings').click();
  await page.waitForFunction(() => !document.getElementById('apiSettingsFields').disabled);
  assert.equal(await page.locator('#deepseekModel').inputValue(), 'ui-configured-model');
  assert.equal(await page.locator('#deepseekApiKey').inputValue(), '');
  await page.locator('[data-clear="deepseek_api_key"]').check();
  await page.locator('#btnApiSettingsSave').click();
  await page.locator('#apiSettingsMessage').filter({ hasText: '立即生效' }).waitFor();
  assert.equal(
    (await page.evaluate(() => fetch('/api/health').then((r) => r.json()))).services.chat,
    false,
  );
  await page.locator('#deepseekApiKey').fill('ui-replacement-key');
  await page.locator('#btnApiSettingsSave').click();
  await page.locator('#apiSettingsMessage').filter({ hasText: '立即生效' }).waitFor();
  await page.locator('#btnApiSettingsClose').click();
  passed('API panel draft testing, live save, redaction, reload and explicit key removal');

  await phone.locator('#btnMenu').click();
  await phone.locator('#btnApiSettings').click();
  await phone.waitForFunction(() => !document.getElementById('apiSettingsFields').disabled);
  assert.equal(await phone.locator('#btnMenu').getAttribute('aria-expanded'), 'false');
  assert.equal(
    await phone.evaluate(
      () =>
        document.getElementById('apiSettingsDialog').getBoundingClientRect().width <= innerWidth,
    ),
    true,
  );
  await phone.screenshot({ path: path.join(artifacts, 'mobile-api-settings.png'), fullPage: true });
  await phone.locator('#btnApiSettingsClose').click();
  passed('mobile API configuration dialog');

  const degraded = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const setup = await degraded.newPage();
  setup.on('pageerror', (error) => errors.push(error.message));
  await setup.route('**/api/health', (route) =>
    route.fulfill({
      json: {
        status: 'degraded',
        services: { chat: false, search: false, voice: false },
        version: '2.0.0',
        limits: { message_chars: 12000 },
      },
    }),
  );
  await setup.goto(base);
  await setup.locator('#serviceNotice').waitFor({ state: 'visible' });
  await setup.locator('#messageInput').fill('测试未配置服务');
  assert.equal(await setup.locator('#btnSend').isDisabled(), true);
  assert.ok((await setup.locator('#serviceNotice').innerText()).includes('配置'));
  assert.ok((await setup.locator('#helpService').textContent()).includes('DEEPSEEK_API_KEY'));
  passed('unconfigured service state remains usable and actionable');

  assert.deepEqual(errors, [], `Browser errors: ${errors.join('; ')}`);
  console.log(`\n${results.length} browser checks passed. Screenshots: ${artifacts}`);
} catch (error) {
  console.error(error);
  if (logs) console.error(logs.slice(-4000));
  process.exitCode = 1;
} finally {
  await browser?.close();
  if (server) server.kill();
}
