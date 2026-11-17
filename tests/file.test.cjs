const {test} = require('node:test');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const {start,edit,problems} = require('./helper.cjs');
test('file URL: editing, storage and sending', async t => {
  const {chromium} = require('playwright');
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage();
    const errors = problems(page);
    await page.goto(pathToFileURL(path.resolve('tests/fixtures/file.html')).href);
    await start(page); await edit(page);
    assert.equal(await page.locator('#dup2').textContent(), 'Fresh bread');
    await page.reload();
    assert.equal(await page.locator('#dup2').textContent(), 'Fresh bread');
    await page.locator('#fixcopy-send').click();
    await page.locator('#fixcopy-panel').waitFor();
    const mode = await page.locator('#fixcopy-note').getAttribute('data-mode');
    assert.ok(['copied','manual'].includes(mode));
    t.diagnostic('file URL: editing works; persistence works; send outcome=' + mode);
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});
