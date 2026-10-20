import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { formatOpeningCurrency } from './opening-currency.ts';

test('MXN opening context overrides an unrelated cached rupee symbol', () => {
  globalThis.localStorage = { getItem: () => '₹' };
  try {
    assert.equal(formatOpeningCurrency(0, 'MXN', 'en-US'), 'MX$0.00');
    assert.equal(formatOpeningCurrency(25, 'MXN', 'es-MX'), '$25.00');
  } finally {
    delete globalThis.localStorage;
  }
});

test('uses the actual context currency and locale, including genuine INR', () => {
  assert.equal(formatOpeningCurrency(0, 'USD', 'en-US'), '$0.00');
  assert.equal(formatOpeningCurrency(0, 'INR', 'en-US'), '₹0.00');
  assert.match(formatOpeningCurrency(0, 'EUR', 'fr-FR'), /0,00.*€/u);
});

test('missing currency never invents a rupee or another currency', () => {
  assert.equal(formatOpeningCurrency(0, null, 'en-US'), '0');
  assert.equal(formatOpeningCurrency(0, undefined, 'fr-FR'), '0');
});

test('both existing warning translations interpolate the formatted amount', () => {
  for (const locale of ['en', 'fr']) {
    const messages = JSON.parse(readFileSync(new URL(`../i18n/locales/${locale}.json`, import.meta.url)));
    const warning = messages.pos_opening.zero_balance_warning;
    assert.equal(warning.split('{{amount}}').length, 2);
    assert.doesNotMatch(warning, /₹/u);
    const output = warning.replace('{{amount}}', formatOpeningCurrency(0, 'MXN', locale));
    assert.match(output, /MX/u);
    assert.doesNotMatch(output, /\{\{|₹/u);
  }
});
