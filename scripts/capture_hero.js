'use strict';
// Photographs the README hero's three panels from a running Home Assistant:
// one entry's device page (its Sensors card), the six CAP Alerts triggers in
// the Add trigger dialog, and the "Alert issued" sidebar of an automation that
// is only ever held in the editor's memory. Nothing is saved to the instance.
//
// Usage:
//   HA_TOKEN=... node scripts/capture_hero.js --device <device_id> --out <dir>
//
// Then scripts/compose_hero.py <dir> builds docs/img/hero-*. Pick an entry in
// English with three or more live alerts and a spread of severities (#395).
//
// Env: HA_URL (default http://localhost:8123), HA_TOKEN (long-lived token),
// PLAYWRIGHT (module path; default the card repo's node_modules, which is
// where the card's own capture-editor.js gets it).
const { mkdirSync } = require('fs');
const { resolve } = require('path');

const PLAYWRIGHT = process.env.PLAYWRIGHT
  || resolve(__dirname, '../../weather_alerts_card/node_modules/playwright');
const { chromium } = require(PLAYWRIGHT);

const HA_URL = process.env.HA_URL || 'http://localhost:8123';
const HA_TOKEN = process.env.HA_TOKEN;

function arg(name) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : undefined;
}
const DEVICE = arg('device');
const OUT = resolve(arg('out') || '.');
const THEMES = ['light', 'dark'];
const TRIGGERS = /^\s*Alert (ended|escalated|issued|updated|downgraded|starts)\b/;

const AUTOMATION = (device) => ({
  alias: 'Severe weather to phone',
  description: '',
  triggers: [{
    trigger: 'cap_alerts.incident_arrived',
    target: { device_id: device },
    options: { threshold: 'severe' },
  }],
  conditions: [],
  actions: [{
    action: 'notify.notify',
    data: { title: '{{ trigger.event }}', message: '{{ trigger.headline }}' },
  }],
  mode: 'queued',
});

// The editor's content is slotted, so the walk visits each element's light
// DOM as well as its shadow root.
const DEEP = `
  window.__deepAll = function (pred, root = document, out = []) {
    const roots = root.shadowRoot ? [root.shadowRoot, root] : [root];
    for (const r of roots) for (const el of r.querySelectorAll('*')) {
      if (pred(el)) out.push(el);
      if (el.shadowRoot) window.__deepAll(pred, el.shadowRoot, out);
    }
    return out;
  };
  window.__first = (name, root) => window.__deepAll(el => el.localName === name, root)[0] || null;
`;

async function openPage(browser, theme, width, path) {
  const dark = theme === 'dark';
  const context = await browser.newContext({
    viewport: { width, height: 1400 }, deviceScaleFactor: 2, colorScheme: theme,
  });
  const page = await context.newPage();
  // A registered service worker serves a cached frontend and ignores the theme pin.
  await page.addInitScript(() => {
    if (navigator.serviceWorker) {
      Object.defineProperty(navigator.serviceWorker, 'register', { value: () => new Promise(() => {}) });
    }
  });
  await page.addInitScript(({ url, token }) => {
    localStorage.setItem('hassTokens', JSON.stringify({
      access_token: token, token_type: 'Bearer', expires_in: 1800,
      expires: Date.now() + 365 * 24 * 3600 * 1000, hassUrl: url, clientId: url + '/',
    }));
    localStorage.setItem('dockedSidebar', JSON.stringify('always_hidden'));
  }, { url: HA_URL, token: HA_TOKEN });
  await page.addInitScript(DEEP);
  await page.goto(`${HA_URL}${path}`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => {
    const ha = document.querySelector('home-assistant');
    return !!ha?.hass?.themes && ha.hass.selectedTheme !== null;
  }, undefined, { timeout: 30000 });
  // Pin the theme in memory only: the user's stored theme is never written.
  await page.evaluate(dark => {
    const ha = document.querySelector('home-assistant');
    ha._updateHass({ selectedTheme: { theme: 'default', dark }, dockedSidebar: 'always_hidden' });
    ha._applyTheme(matchMedia('(prefers-color-scheme: dark)').matches);
  }, dark);
  await page.waitForFunction(dark => document.querySelector('home-assistant').hass.themes.darkMode === dark, dark, { timeout: 10000 });
  return { context, page };
}

async function settled(page) {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(2500);
}

async function deviceSensors(browser, theme) {
  const { context, page } = await openPage(browser, theme, 1180, `/config/devices/device/${DEVICE}`);
  await page.waitForFunction(() => {
    const p = window.__first('ha-config-device-page');
    return p && window.__deepAll(el => el.localName === 'ha-device-entities-card', p).length > 0;
  }, undefined, { timeout: 30000 });
  await settled(page);
  await page.setViewportSize({ width: 1180, height: 2400 });
  await page.waitForTimeout(800);
  // The first entities card is Sensors, where the alert entities live.
  const clip = await page.evaluate(() => {
    const card = window.__deepAll(el => el.localName === 'ha-device-entities-card')
      .find(c => c.getBoundingClientRect().height > 0);
    const r = card.getBoundingClientRect();
    return { x: r.x - 12, y: r.y - 12, width: r.width + 24, height: r.height + 24 };
  });
  await page.screenshot({ path: resolve(OUT, `sensors-${theme}.png`), clip });
  await context.close();
}

async function triggerList(browser, theme) {
  // 470 px is the dialog width at which the list scales cleanly to the Sensors
  // card's width in the composite.
  const { context, page } = await openPage(browser, theme, 470, '/config/automation/edit/new');
  await page.waitForFunction(() => !!window.__first('ha-automation-trigger'), undefined, { timeout: 30000 });
  await page.waitForTimeout(1500);
  await page.evaluate(() => {
    window.__deepAll(el => el.localName === 'ha-button', window.__first('ha-automation-trigger'))[0].click();
  });
  await page.waitForFunction(() => {
    const d = window.__first('add-automation-element-dialog');
    return d && window.__deepAll(el => el.localName === 'input', d).length > 0;
  }, undefined, { timeout: 30000 });
  await page.waitForTimeout(1500);
  const input = await page.evaluateHandle(() => window.__deepAll(el => el.localName === 'input', window.__first('add-automation-element-dialog'))[0]);
  await input.focus();
  await page.keyboard.type('alert');
  await page.waitForFunction(re => window.__deepAll(el => el.localName === 'ha-list-item-button' || el.localName === 'ha-dropdown-item')
    .filter(e => new RegExp(re).test(e.textContent || '')).length === 6, TRIGGERS.source, { timeout: 30000 });
  await settled(page);
  await page.setViewportSize({ width: 470, height: 1100 });
  await page.waitForTimeout(800);
  const { clip, icons } = await page.evaluate(re => {
    const d = window.__first('add-automation-element-dialog');
    const rows = window.__deepAll(el => (el.localName === 'ha-list-item-button' || el.localName === 'ha-dropdown-item')
      && new RegExp(re).test(el.textContent || ''), d);
    // An icon that resolved renders a path; a missing icons.json entry (#394)
    // renders an empty ha-svg-icon.
    const icons = rows.filter(r => window.__deepAll(el => el.localName === 'path' && el.getAttribute('d'), r).length > 0).length;
    const rs = rows.map(r => r.getBoundingClientRect());
    const y = Math.min(...rs.map(r => r.y)), y2 = Math.max(...rs.map(r => r.y + r.height));
    const x = Math.min(...rs.map(r => r.x)), x2 = Math.max(...rs.map(r => r.x + r.width));
    return { clip: { x: x - 4, y: y - 44, width: x2 - x + 8, height: y2 - y + 46 }, icons };
  }, TRIGGERS.source);
  console.log(`  [${theme}] trigger rows with an icon: ${icons}/6`);
  if (icons !== 6) throw new Error('trigger icons missing: is the instance running 0.8.1 or later?');
  await page.screenshot({ path: resolve(OUT, `triggers-${theme}.png`), clip });
  await context.close();
}

async function triggerSidebar(browser, theme) {
  const { context, page } = await openPage(browser, theme, 1180, '/config/automation/edit/new');
  await page.waitForFunction(() => !!window.__first('manual-automation-editor'), undefined, { timeout: 30000 });
  await page.waitForTimeout(1500);
  // Setting the editor's config property does not re-render; the event does.
  await page.evaluate(cfg => {
    window.__first('manual-automation-editor').dispatchEvent(
      new CustomEvent('value-changed', { detail: { value: cfg }, bubbles: true, composed: true }));
  }, AUTOMATION(DEVICE));
  await page.waitForFunction(() => window.__deepAll(el => el.localName === 'ha-automation-row-targets').length > 0, undefined, { timeout: 30000 });
  await page.waitForTimeout(1500);
  await page.evaluate(() => {
    const row = window.__first('ha-automation-trigger-row');
    (window.__first('ha-automation-row', row) || row).click();
  });
  await page.waitForFunction(() => {
    const sb = window.__first('ha-automation-sidebar-card');
    return sb && window.__first('ha-target-picker-item-row', sb) && window.__first('ha-selector-select', sb);
  }, undefined, { timeout: 30000 });
  await settled(page);
  await page.setViewportSize({ width: 1180, height: 760 });
  await page.waitForTimeout(800);
  const clip = await page.evaluate(() => {
    const r = window.__first('ha-automation-sidebar-card').getBoundingClientRect();
    return { x: r.x - 8, y: r.y - 8, width: r.width + 16, height: r.height + 16 };
  });
  await page.screenshot({ path: resolve(OUT, `sidebar-${theme}.png`), clip });
  await context.close();
}

(async () => {
  if (!HA_TOKEN) throw new Error('HA_TOKEN unset');
  if (!DEVICE) throw new Error('--device <device_id> is required');
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  try {
    for (const theme of THEMES) {
      console.log(`[${theme}]`);
      await deviceSensors(browser, theme);
      await triggerList(browser, theme);
      await triggerSidebar(browser, theme);
    }
  } finally {
    await browser.close();
  }
  console.log(`panels in ${OUT}`);
})().catch(err => { console.error(err); process.exit(1); });
