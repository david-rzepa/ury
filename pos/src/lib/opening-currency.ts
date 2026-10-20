/** Use the opening context's company currency, never a cached symbol/default. */
export function formatOpeningCurrency(amount: number, currency?: string | null, locale = 'en'): string {
  return new Intl.NumberFormat(locale, currency ? { style: 'currency', currency } : {}).format(amount);
}
