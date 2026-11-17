const { test, before, after, describe } = require('node:test');
const assert = require('node:assert/strict');
const { setup, start, edit, problems } = require('./helper.cjs');
describe("browser behaviour", () => {
let env;
before(async () => {
  try { env = await setup(); } catch (err) { console.error(err.message); throw err; }
});
after(async () => { if (env) await env.close(); });
async function open(name = 'en', options = {}, init) {
  const context = await env.browser.newContext(options);
  if (init) await context.addInitScript(init);
  const page = await context.newPage();
  page.errors = problems(page);
  await page.goto(env.base + '/' + name + '.html');
  return page;
}
async function done(page) { assert.deepEqual(page.errors, []); await page.context().close(); }
test('self enabling, head/body placement, duplicate load and metadata defaults', async () => {
  for (const name of ['en','head']) {
    const p = await open(name);
    assert.equal(await p.locator('#fixcopy-title').textContent(), '✎ Fix wording');
    await p.addScriptTag({ url: '/fixcopy.js' });
    assert.equal(await p.locator('#fixcopy-bar').count(), 1);
    await done(p);
  }
  for (const content of ['', 'on', '{broken']) {
    const p = await open();
    await p.route('**/defaults.html', route => route.fulfill({contentType:'text/html; charset=utf-8', body:
      '<html lang="en"><head><meta name="fixcopy" content="' + content +
      '"></head><body><p>Sample wording</p><script src="/fixcopy.js"></script></body></html>'}));
    await p.goto(env.base + '/defaults.html');
    assert.equal(await p.locator('#fixcopy-title').textContent(), '✎ Fix wording');
    await done(p);
  }
});
test('language selection: page, configuration and navigator', async () => {
  for (const [html, configured, locale, expected] of [
    ['ja', null, 'en-US', '文言を直す'], ['en',null,'ja-JP','Fix wording'],
    ['en','ja','en-US','文言を直す'], ['ja','en','ja-JP','Fix wording'],
    ['',null,'ja-JP','文言を直す'], ['',null,'en-GB','Fix wording'],
    ['fr','invalid','ja-JP','Fix wording']
  ]) {
    const p = await open('en', {locale});
    await p.route('**/language.html', route => route.fulfill({contentType:'text/html; charset=utf-8', body:
      '<html lang="' + html + '"><head><meta name="fixcopy" content=\'' + JSON.stringify({lang:configured}) +
      '\'></head><body><p>Sample wording</p><script src="/fixcopy.js"></script></body></html>'}));
    await p.goto(env.base + '/language.html');
    assert.ok((await p.locator('#fixcopy-title').textContent()).includes(expected));
    await done(p);
  }
});
test('editing, both reports, persistence, folding, reset and exclusions', async () => {
  for (const lang of ['en','ja']) {
    const p = await open(lang, {permissions:['clipboard-read','clipboard-write']});
    await start(p);
    assert.equal(await p.locator('#protected').getAttribute('contenteditable'), null);
    await edit(p);
    await p.locator('#label').click();
    await p.locator('#fixcopy-svg-input').fill('Wrapped');
    await p.keyboard.press('Enter');
    assert.match(await p.locator('#fixcopy-status').textContent(), /2/);
    await p.locator('#fixcopy-send').click();
    const report = await p.evaluate(() => navigator.clipboard.readText());
    assert.equal(report, lang === 'en'
      ? 'Bread basket — Wording changes\n\n1. Before (occurrence 2 of 2; under “Our menu”): Baked daily\n   After: Fresh bread\n\n2. (figure) Before: Packing\n   After: Wrapped'
      : 'パンのかご 文言の直し\n\n1. 元（2か所目／全2か所・見出し「今日のメニュー」の下）：毎朝焼きます\n   新：Fresh bread\n\n2. （図）元：袋詰め\n   新：Wrapped');
    assert.equal(await p.locator('#fixcopy-status').textContent(), lang === 'en' ? 'Changes: 2 · Copied' : '2 か所 ・ 送る文章はコピー済みです');
    assert.equal(await p.locator('#fixcopy-send').textContent(), lang === 'en' ? 'Send your changes (2)' : '直した内容を送る（2 か所）');
    const stored = await p.evaluate(() => JSON.parse(localStorage.getItem('fixcopy:' + location.pathname)));
    assert.equal(stored.v, 1); assert.equal(stored.closed, false); assert.equal(typeof stored.sent, 'string');
    assert.deepEqual(stored.edits.map(e => [e.i, e.n, !!e.s, e.t]), [[2, 2, false, 'Fresh bread'], [1, 1, true, 'Wrapped']]);
    await p.reload();
    assert.equal(await p.locator('#dup2').textContent(), 'Fresh bread');
    assert.equal(await p.locator('#label').textContent(), 'Wrapped');
    await p.locator('#fixcopy-bar button[aria-label]').click();
    assert.equal(await p.locator('#fixcopy-bar').isVisible(), false);
    assert.equal(await p.locator('#fixcopy-handle').isVisible(), true);
    await p.reload();
    assert.equal(await p.locator('#fixcopy-bar').isVisible(), false);
    assert.equal(await p.locator('#fixcopy-handle').isVisible(), true);
    assert.equal(await p.evaluate(() => JSON.parse(localStorage.getItem('fixcopy:' + location.pathname)).closed), true);
    await p.locator('#fixcopy-handle').click();
    assert.equal(await p.locator('#fixcopy-bar').isVisible(), true);
    // Declining the confirm dialog leaves the edits alone.
    p.once('dialog', async d => { await d.dismiss(); });
    await p.getByRole('button', {name: lang === 'en' ? 'Reset changes' : '元に戻す', exact:true}).click();
    assert.equal(await p.locator('#dup2').textContent(), 'Fresh bread');
    p.once('dialog', async d => { assert.equal(d.type(), 'confirm'); assert.equal(d.message(), lang === 'en' ? 'Reset all your wording changes?' : '直した文言をすべて元に戻します。よろしいですか？'); await d.accept(); });
    await p.getByRole('button', {name: lang === 'en' ? 'Reset changes' : '元に戻す', exact:true}).click();
    assert.equal(await p.locator('#dup2').textContent(), lang === 'en' ? 'Baked daily' : '毎朝焼きます');
    assert.equal(await p.locator('#label').textContent(), lang === 'en' ? 'Packing' : '袋詰め');
    await done(p);
  }
});
test('settings, manual fallback and unavailable APIs', async () => {
  const p = await open();
  await p.route('**/settings.html', async route => {
    const fs = require('node:fs');
    await route.fulfill({contentType:'text/html; charset=utf-8', body:fs.readFileSync('tests/fixtures/en.html','utf8').replace('</head>',
      '<meta name="fixcopy" content=\'{"title":"Bread notes","send_to":"Send to the bakery","skip":".skip"}\'></head>')});
  });
  await p.addInitScript(() => {
    Object.defineProperty(navigator,'clipboard',{get(){ throw new Error('Unavailable'); }});
    Object.defineProperty(navigator,'share',{value:undefined});
    document.execCommand = () => false;
    Object.defineProperty(window,'localStorage',{get(){ throw new Error('Unavailable'); }});
  });
  await p.goto(env.base + '/settings.html');
  await start(p);
  assert.equal(await p.locator('.skip').getAttribute('contenteditable'), null);
  assert.match(await p.locator('#fixcopy-guide').textContent(), /Send to the bakery/);
  await edit(p);
  await p.locator('#fixcopy-send').click();
  assert.equal(await p.locator('#fixcopy-panel textarea').inputValue().then(s => s.startsWith('Bread notes')), true);
  assert.match(await p.locator('#fixcopy-note').textContent(), /Select/);
  await done(p);
});
test('phone share, cancellation, no overflow, SVG keyboard and composition', async () => {
  for (const cancel of [false,true]) {
    const p = await open('en', {hasTouch:true,isMobile:true,viewport:{width:390,height:844}}, cancel ? () => {
      navigator.share = () => Promise.reject(new DOMException('Cancelled','AbortError'));
    } : () => { navigator.share = data => { window.shared = data; return Promise.resolve(); }; });
    await start(p);
    assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= 390), true);
    await p.locator('#label').click();
    const input = p.locator('#fixcopy-svg-input');
    await input.fill('New label');
    await input.dispatchEvent('keydown',{key:'Enter',isComposing:true});
    assert.equal(await input.count(), 1);
    await p.keyboard.press('Escape');
    assert.equal(await p.locator('#label').textContent(), 'Packing');
    await p.locator('#label').click(); await input.fill(''); await p.keyboard.press('Enter');
    assert.equal(await p.locator('#label').textContent(), 'Packing');
    await p.locator('#span2').click(); await input.fill('Packed'); await p.keyboard.press('Enter');
    assert.equal(await p.locator('#span1').textContent(), 'Ready');
    await p.locator('#fixcopy-send').click();
    if (cancel) assert.match(await p.locator('#fixcopy-note').textContent(), /cancelled/);
    else assert.match(await p.evaluate(() => window.shared.text), /\(figure\).*occurrence 2 of 2/);
    assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= 390), true);
    await done(p);
  }
});

test('inline script waits for the body and preserves line breaks', async () => {
  const p = await open();
  const script = require('node:fs').readFileSync('fixcopy.js', 'utf8');
  await p.route('**/inline.html', route => route.fulfill({contentType:'text/html; charset=utf-8', body:
    '<html lang="en"><head><script>' + script + '</script></head><body><p id="words">First<br>Second</p></body></html>'}));
  await p.goto(env.base + '/inline.html');
  await start(p);
  assert.equal(await p.locator('#words').getAttribute('contenteditable'), 'plaintext-only');
  // A real Enter key in a plaintext-only element inserts a newline; it must become a <br>.
  await p.locator('#words').focus();
  await p.evaluate(() => { const n = document.getElementById('words'), r = document.createRange(), s = getSelection();
    r.selectNodeContents(n); r.collapse(false); s.removeAllRanges(); s.addRange(r); });
  await p.keyboard.press('Enter'); await p.keyboard.type('Third');
  await p.locator('#words').blur();
  assert.equal(await p.locator('#words br').count(), 2);
  assert.equal(await p.locator('#words').evaluate(n => n.innerHTML), 'First<br>Second<br>Third');
  await p.locator('#fixcopy-send').click({ trial: true });
  assert.match(await p.locator('#fixcopy-status').textContent(), /^(1 |Changes: 1 )/);
  await done(p);
});
test('async clipboard refusal falls through legacy copy to a manual sheet', async () => {
  const p = await open('en', {}, () => {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('Refused')) } });
    document.execCommand = () => false;
  });
  await start(p); await edit(p);
  await p.locator('#fixcopy-send').click();
  assert.equal(await p.locator('#fixcopy-note').getAttribute('data-mode'), 'manual');
  assert.match(await p.locator('#fixcopy-panel textarea').inputValue(), /After: Fresh bread/);
  await done(p);
});
test('SVG retains author attributes, edge spaces and excludes hidden definitions', async () => {
  const p = await open();
  await p.evaluate(() => {
    document.querySelector('#label').setAttribute('tabindex','-1');
    document.querySelector('#label').setAttribute('role','link');
  });
  // Reload with attributes in the source so initialization can capture them.
  const source = require('node:fs').readFileSync('tests/fixtures/en.html', 'utf8')
    .replace('id="label"', 'id="label" tabindex="-1" role="link"').replace('>Packing<', '> Packing <');
  await p.route('**/attributes.html', route => route.fulfill({contentType:'text/html; charset=utf-8',body:source}));
  await p.goto(env.base + '/attributes.html');
  await start(p);
  assert.equal(await p.locator('svg title').getAttribute('data-fixcopy-editable'), null);
  assert.equal(await p.locator('svg defs text').getAttribute('data-fixcopy-editable'), null);
  await p.locator('#label').focus(); await p.keyboard.press('Space');
  await p.locator('#fixcopy-svg-input').fill('Packed');
  await p.keyboard.press('Enter');
  assert.equal(await p.locator('#label').textContent(), ' Packed ');
  await p.getByRole('button',{name:'Finish editing',exact:true}).click();
  assert.equal(await p.locator('#label').getAttribute('role'), 'link');
  assert.equal(await p.locator('#label').getAttribute('tabindex'), '-1');
  assert.equal(await p.locator('svg rect.fixcopy-box.changed').count(), 1);
  await done(p);
});

test('SVG: blur commits, keyCode 229 does not, separate counters, box styling and print', async () => {
  const p = await open();
  const source = require('node:fs').readFileSync('tests/fixtures/en.html', 'utf8')
    .replace('<p id="lines">', '<p id="ready">Ready</p><p id="lines">');
  await p.route('**/counters.html', route => route.fulfill({contentType:'text/html; charset=utf-8', body:source}));
  await p.goto(env.base + '/counters.html');
  await start(p);
  // One dashed box per label (2 labels + 2 tspans), inserted before its text element.
  assert.equal(await p.locator('svg rect.fixcopy-box').count(), 3);
  assert.equal(await p.locator('#label').evaluate(n => n.previousElementSibling.classList.contains('fixcopy-box')), true);
  assert.equal(await p.locator('svg rect.fixcopy-box').first().evaluate(n => getComputedStyle(n).vectorEffect), 'non-scaling-stroke');
  await p.emulateMedia({ media: 'print' });
  assert.equal(await p.locator('svg rect.fixcopy-box').first().evaluate(n => getComputedStyle(n).display), 'none');
  await p.emulateMedia({ media: 'screen' });
  await p.locator('#span1').click();
  const input = p.locator('#fixcopy-svg-input');
  await input.fill('Done');
  await input.dispatchEvent('keydown', { key: 'Enter', keyCode: 229 });
  assert.equal(await input.count(), 1);
  // Blur commits.
  await p.locator('#fixcopy-title').focus();
  assert.equal(await input.count(), 0);
  assert.equal(await p.locator('#span1').textContent(), 'Done');
  // HTML and SVG duplicates are counted separately: html "Ready" is 1 of 1 (no note) while the two svg ones are 1 of 2.
  await edit(p, '#ready', 'Ok');
  await p.locator('#fixcopy-send').click();
  await p.locator('#fixcopy-panel').waitFor();
  const report = await p.locator('#fixcopy-panel textarea').inputValue();
  assert.equal(report.match(/occurrence/g).length, 1);
  assert.match(report, /\(figure\) Before \(occurrence 1 of 2; under “Our menu”\): Ready\n   After: Done/);
  assert.match(report, /1\. Before: Ready\n   After: Ok/);
  assert.equal(await p.locator('svg rect.fixcopy-box.changed').count(), 1);
  await done(p);
});
test('SVG labels with a zero box or a CSS transform get no dashed box', async () => {
  const p = await open();
  const source = require('node:fs').readFileSync('tests/fixtures/en.html', 'utf8')
    .replace('<text id="label"', '<text id="moved" style="transform:translate(5px,5px)" x="10" y="90">Moved</text><text id="gone" display="none" x="10" y="100">Gone</text><text id="label"');
  await p.route('**/boxes.html', route => route.fulfill({contentType:'text/html; charset=utf-8', body:source}));
  await p.goto(env.base + '/boxes.html');
  await start(p);
  assert.equal(await p.locator('#moved').evaluate(n => n.previousElementSibling && n.previousElementSibling.classList.contains('fixcopy-box')), false);
  assert.equal(await p.locator('#gone').evaluate(n => n.previousElementSibling && n.previousElementSibling.classList.contains('fixcopy-box')), false);
  assert.equal(await p.locator('#label').evaluate(n => n.previousElementSibling.classList.contains('fixcopy-box')), true);
  await done(p);
});
test('Escape closes the sheet, Space opens a focused label, saved SVG edit uses the s flag', async () => {
  const p = await open('en', { permissions: ['clipboard-read', 'clipboard-write'] });
  await start(p);
  await p.locator('#label').focus(); await p.keyboard.press('Space');
  await p.locator('#fixcopy-svg-input').fill(' Wrapped up ');
  await p.keyboard.press('Enter');
  assert.equal(await p.locator('#label').textContent(), 'Wrapped up');
  assert.equal(await p.evaluate(() => document.activeElement.id), 'label');
  await p.locator('#fixcopy-send').click();
  await p.locator('#fixcopy-panel').waitFor();
  assert.equal(await p.locator('#fixcopy-panel').isVisible(), true);
  await p.keyboard.press('Escape');
  assert.equal(await p.locator('#fixcopy-panel').isVisible(), false);
  assert.equal(await p.evaluate(() => JSON.parse(localStorage.getItem('fixcopy:' + location.pathname)).edits[0].s), 1);
  await done(p);
});

});
