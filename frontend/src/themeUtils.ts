export function getCssVar(name: string): string {
  if (typeof document === 'undefined') return ''
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

export function getChartColors() {
  return {
    bg: getCssVar('--chart-bg'),
    grid: getCssVar('--chart-grid'),
    text: getCssVar('--chart-text'),
    barTrack: getCssVar('--chart-bar-track'),
    tooltipBg: getCssVar('--chart-tooltip-bg'),
    tooltipBorder: getCssVar('--chart-tooltip-border'),
    spending: getCssVar('--chart-spending'),
    income: getCssVar('--chart-income'),
    series: [
      getCssVar('--chart-line-1'),
      getCssVar('--chart-line-2'),
      getCssVar('--chart-line-3'),
      getCssVar('--chart-line-4'),
      getCssVar('--chart-line-5'),
    ],
  }
}
