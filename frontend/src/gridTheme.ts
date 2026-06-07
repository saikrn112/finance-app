import { themeQuartz } from 'ag-grid-community'
import { getCssVar } from './themeUtils'

export function getGridTheme() {
  return themeQuartz.withParams({
    backgroundColor: getCssVar('--grid-bg'),
    foregroundColor: getCssVar('--grid-text'),
    headerBackgroundColor: getCssVar('--grid-header-bg'),
    borderColor: getCssVar('--grid-border'),
    rowHoverColor: getCssVar('--grid-row-hover'),
    selectedRowBackgroundColor: getCssVar('--grid-row-selected'),
  })
}
