const API_BASE = '/api'

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(message: string, status: number, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init)
  const contentType = res.headers.get('content-type') || ''
  const isJson = contentType.includes('application/json')
  const body = isJson ? await res.json().catch(() => null) : await res.text().catch(() => '')

  if (!res.ok) {
    const message =
      typeof body === 'object' && body && 'detail' in body && typeof body.detail === 'string'
        ? body.detail
        : typeof body === 'string' && body
          ? body
          : `API error: ${res.status}`
    throw new ApiError(message, res.status, body)
  }

  return body as T
}

type CacheEntry<T> = {
  value?: T
  promise?: Promise<T>
  expiresAt: number
}

const transientCache = new Map<string, CacheEntry<unknown>>()
const VAULT_CACHE_TTL_MS = 300_000

function invalidateCache(keyPrefix: string) {
  for (const key of transientCache.keys()) {
    if (key === keyPrefix || key.startsWith(`${keyPrefix}:`)) {
      transientCache.delete(key)
    }
  }
}

async function cachedRequest<T>(key: string, loader: () => Promise<T>, ttlMs = VAULT_CACHE_TTL_MS): Promise<T> {
  const now = Date.now()
  const cached = transientCache.get(key) as CacheEntry<T> | undefined
  if (cached?.value !== undefined && cached.expiresAt > now) return cached.value
  if (cached?.promise) return cached.promise

  const promise = loader()
    .then((value) => {
      transientCache.set(key, { value, expiresAt: Date.now() + ttlMs })
      return value
    })
    .catch((error) => {
      transientCache.delete(key)
      throw error
    })

  transientCache.set(key, { promise, expiresAt: now + ttlMs })
  return promise
}

export async function fetchJson<T>(path: string): Promise<T> {
  return request<T>(path)
}

export interface Transaction {
  id: string
  date: string
  authorized_date: string | null
  effective_date: string
  amount: number
  currency?: string
  merchant_raw: string
  merchant_clean: string
  original_description: string | null
  category: string
  source: string
  account_last4: string | null
  pending: boolean
  is_recurring: boolean
  tags: string[]
  notes?: string | null
  projects: { id: string; name: string; color: string }[]
  description?: string
  splits?: TransactionSplit[]
  split_mode?: 'equal' | 'unequal'
}

export interface TransactionCategoryOption {
  category: string
  subcategories: string[]
}

export interface UncategorizedReviewTransaction extends Transaction {}

export interface UncategorizedReviewGroup {
  merchant_key: string
  display_name: string
  merchant_clean_suggestion: string
  rule_pattern_source: string
  transaction_count: number
  total_amount: number
  latest_date: string | null
  transactions: UncategorizedReviewTransaction[]
}

export interface UncategorizedReviewResponse {
  groups: UncategorizedReviewGroup[]
  total_groups: number
  total_transactions: number
  page: number
  limit: number
  total_pages: number
}

export interface UncategorizedApplyRequest {
  merchant_key: string
  transaction_ids: string[]
  category: string
  merchant_clean?: string
  rule_pattern_source?: string
  apply_to_similar?: boolean
  save_rule?: boolean
}

export interface UncategorizedApplyResult {
  updated_count: number
  updated_ids: string[]
  category: string
  rule_saved: boolean
  undo_payload: Array<{
    id: string
    category: string | null
    merchant_clean: string | null
    category_source: string | null
  }>
}

export interface UncategorizedUndoResult {
  updated_count: number
  updated_ids: string[]
}

export interface Summary {
  income: number
  spending: number
  core_spending: number
  core_spending_excluding_rent: number
  transfers: number
  spend_income_ratio: number
  net_flow: number
  subscriptions_monthly: number
  range: string
}

export interface CategoryData {
  category: string
  total: number
}

export interface MerchantData {
  merchant: string
  total: number
}

export interface TrendData {
  period: string
  total: number
}

export interface SubscriptionData {
  merchant: string
  amount: number
  frequency: string
  last_charge: string
  occurrences: number
  status: string
  next_expected?: string
  source?: string
  account_last4?: string | null
  monthly_equivalent?: number
  flags?: string[]
}

export interface RecurringCatalogSummary {
  current_monthly_total: number
  active_count: number
  ended_in_range: number
  price_changes_in_range: number
  renewing_soon: number
}

export interface RecurringCatalogItem {
  id: string
  display_name: string
  canonical_name: string
  status: 'active' | 'ended' | 'paused'
  kind: 'subscription' | 'bill' | 'renewal'
  category: string
  top_category: string
  currency: string
  current_amount: number
  monthly_equivalent: number
  frequency: string
  current_source: string
  current_account_last4: string | null
  first_seen: string
  last_seen: string
  next_expected: string
  ended_at: string | null
  latest_event_type: string | null
  flags: string[]
  detection_mode: string
  event_count: number
}

export interface RecurringCatalogResponse {
  start_date: string
  end_date: string
  summary: RecurringCatalogSummary
  items: RecurringCatalogItem[]
  filters: {
    statuses: string[]
    kinds: string[]
    sources: string[]
    categories: string[]
    currencies: string[]
  }
}

export interface RecurringTrendGroup {
  key: string
  label: string
  total: number
}

export interface RecurringTrendPoint {
  date: string
  label: string
  total: number
  breakdown: Record<string, number>
}

export interface RecurringTrendsResponse {
  start_date: string
  end_date: string
  metric: string
  group_by: string
  groups: RecurringTrendGroup[]
  points: RecurringTrendPoint[]
}

export interface RecurringEvent {
  event_type: string
  effective_date: string
  amount: number
  frequency: string
  source: string
  account_last4: string | null
  transaction_id: string | null
  metadata?: Record<string, unknown>
}

export interface RecurringLinkedTransaction {
  transaction_id: string
  date: string
  amount: number
  source: string
  account_last4: string | null
  merchant: string
  category: string
}

export interface RecurringDetail extends RecurringCatalogItem {
  events: RecurringEvent[]
  transactions: RecurringLinkedTransaction[]
  price_history: { date: string; amount: number }[]
  source_history: { date: string; source: string; account_last4: string | null }[]
}

export interface ProjectSummary {
  id: string
  name: string
  color: string
  start_date: string | null
  end_date: string | null
  budget: number | null
  status: string
  notes: string | null
  spent: number
  txn_count: number
  created_at: string | null
  earliest_transaction_date?: string | null
  latest_transaction_date?: string | null
}

export interface ProjectCategoryBreakdown {
  category: string
  total: number
  subcategories: { name: string; total: number }[]
}

export interface TransactionSplit {
  id: string
  name: string
  color: string
  /** This person's explicit share in the display currency. null = split equally. */
  share_amount?: number | null
}

export interface MemberTotal {
  id: string
  name: string
  color: string
  expenditure: number
  income: number
  net: number
}

export interface SplitwiseCredentialsInfo {
  usable: boolean
  auth_mode: 'api_key' | 'oauth' | null
  client_id_present: boolean
  client_id_hint: string | null
  client_secret_present: boolean
  api_key_present: boolean
  redirect_uri: string
  client_id_source: 'app' | 'config' | null
  client_secret_source: 'app' | 'config' | null
  api_key_source: 'app' | null
}

export interface SplitwiseStatus {
  configured: boolean
  credentials: SplitwiseCredentialsInfo
  connected: boolean
  account_name: string | null
  account_email: string | null
  self_contact: { id: string; name: string } | null
  batch_size: number
}

export interface SplitwiseFriend {
  id: string
  name: string
  email: string | null
  already_linked: boolean
}

export interface SplitwiseContactLink {
  id: string
  name: string
  color: string
  is_self: boolean
  splitwise_user_id: string | null
  splitwise_name: string | null
}

export interface SplitwiseCommitSummary {
  project_id: string
  transactions: number
  committed: number
  pending: number
  failed: { transaction_id: string; error: string }[]
  batch_size: number
  unmapped_members: string[]
  connected?: boolean
  created?: number
  updated?: number
  skipped?: number
  batch_failures?: { transaction_id: string; error: string }[]
  group_id?: string | null
  done?: boolean
}

export interface Contact {
  id: string
  name: string
  color: string
}

export interface ProjectDetail extends ProjectSummary {
  categories: ProjectCategoryBreakdown[]
  transactions: Transaction[]
  members: Contact[]
  /** Server-computed per-person totals; authoritative over any local recomputation. */
  member_totals?: MemberTotal[]
}

export interface NetWorthHistoryPoint {
  date: string
  bank_accounts: number
  credit_cards: number
  cash_like: number
  brokerage: number
  retirement: number
  tracked_total: number
  total: number
}

export interface NetWorthSourceRow {
  key: string
  label: string
  group: string
  current: number
  history_mode: 'historical' | 'latest_only'
}

export interface NetWorthTrackedHistory {
  start_date: string
  end_date: string
  points: NetWorthHistoryPoint[]
  latest_sources: NetWorthSourceRow[]
  groups: { key: string; label: string; history_mode: 'historical' | 'latest_only' }[]
}

export interface SidebarAccount {
  source: string
  account_last4?: string | null
  account_key?: string
  provider_source?: string
  source_key?: string
  group: 'bank_account' | 'credit_card' | 'investment' | 'retirement'
  connection_state: 'plaid' | 'manual'
  balance: number | null
  ledger_balance: number | null
  snapshot_balance: number | null
  last_synced: string | null
  filter_source: string | null
  icon_url?: string
  currency?: string
}

export interface VaultStatus {
  vault_id: string | null
  provider: string | null
  provider_email: string | null
  connected: boolean
  last_backup_at: string | null
  last_backup_file_id: string | null
  drive_folder_name?: string | null
  drive_folder_id?: string | null
  last_backup_id?: string | null
  last_restore_at?: string | null
  google_drive_ready: boolean
}

export interface VaultBackupRow {
  backup_id: string
  parent_backup_id: string | null
  created_at: string
  device_id: string | null
  device_label: string | null
  archive_name: string | null
  archive_file_id: string | null
  manifest_file_id: string | null
  archive_sha256: string | null
}

export interface VaultDiscoveryRow {
  vault_id: string
  folder_id: string
  latest_backup_id: string | null
  latest_backup_created_at: string | null
}

export interface VaultBackupJob {
  job_id: string
  status: 'running' | 'success' | 'error'
  stage: 'queued' | 'preparing' | 'uploading' | 'finalizing' | 'success' | 'error'
  progress: number
  message: string
  created_at: string
  updated_at: string
  finished_at?: string
  error?: string
  result?: {
    status: string
    provider: string
    vault_id: string
    backup_id: string
    backup_created_at: string
    backup_name: string
    file_id: string | null
    manifest_file_id: string | null
  }
}

export interface VaultRestoreJob {
  job_id: string
  status: 'running' | 'success' | 'error'
  stage: 'downloading' | 'restoring' | 'success' | 'error'
  progress: number
  message: string
  archive_size?: number
  downloaded?: number
  created_at: string
  updated_at: string
  finished_at?: string
  error?: string
  result?: {
    status: string
    vault_id: string
    backup_id: string
    created_at: string
    summary: { files: number; transactions: number | null }
  }
}

export interface SettingsResponse {
  stats: {
    total_transactions: number
    connected_accounts: number
  }
  plaid_usage: {
    month_start: string
    month_end_exclusive: string
    total_estimated_cost: number
    billing_lines: Array<{
      label: string
      quantity: number
      unit: string
      unit_price: number
      estimated_cost: number
    }>
    scope: string
    scope_note: string
    devices: Array<{
      device_key: string
      device_label: string
      environment?: string | null
      database_id?: string | null
      database_name?: string | null
      call_count: number
      balance_calls: number
      endpoints: Record<string, number>
    }>
    endpoints: Array<{
      endpoint: string
      call_count: number
      units: number
      estimated_cost: number
    }>
  }
  vault: VaultStatus
  accounts: Array<{
    id: string
    plaid_item_id?: string | null
    source: string
    status: string
    last_sync: string | null
    last_sync_error?: {
      type?: string | null
      message?: string | null
      code?: string | null
      display_message?: string | null
      documentation_url?: string | null
    } | null
  }>
}

export interface ImportDuplicateReason {
  type: 'source_id' | 'heuristic'
  existing_id: string
  existing_origin: string
  existing_date?: string
  existing_amount?: number
  existing_merchant_raw?: string
}

export interface ImportPreviewItem {
  source: string
  source_id: string
  date: string
  amount: number
  merchant_raw: string
  account_last4: string | null
  merchant_clean?: string | null
  origin: string
  duplicate_reason: ImportDuplicateReason | null
}

export interface ImportDuplicateSummary {
  total_transactions: number
  duplicate_count: number
  importable_count: number
  items: ImportPreviewItem[]
}

export interface ImportPreview {
  import_id: string
  filename: string
  file_hash: string
  source: string
  source_key: string
  kind: 'csv' | 'statement_pdf'
  path: string
  created_at: string
  already_imported: boolean
  transactions: Array<{
    source: string
    source_id: string
    date: string
    amount: number
    merchant_raw: string
    account_last4: string | null
    merchant_clean?: string | null
    origin: string
  }>
  duplicate_summary: ImportDuplicateSummary
}

export interface ImportCommitResult {
  status: 'success' | 'already_imported'
  import_id: string
  imported: number
  skipped: number
  duplicate_count: number
  archived_path?: string
}

function sourceParam(accounts: Set<string> | null | undefined) {
  return accounts ? `&source=${Array.from(accounts).join(',')}` : ''
}
function catParam(category: string | null | undefined) {
  return category ? `&category=${encodeURIComponent(category)}` : ''
}
function coreExpensesParam(coreExpensesOnly?: boolean, includeRent = true) {
  return coreExpensesOnly ? `&core_expenses_only=1&include_rent=${includeRent ? '1' : '0'}` : ''
}
function currencyParam(currency?: string | null) {
  return currency ? `&currency=${encodeURIComponent(currency)}` : `&currency=USD`
}
function currencyParamRequired(currency: string) {
  return `&currency=${encodeURIComponent(currency)}`
}

export const api = {
  getSummary: (start: string, end: string, accounts?: Set<string> | null, category?: string | null, currency?: string | null) => fetchJson<Summary>(`/analytics/summary?start_date=${start}&end_date=${end}${sourceParam(accounts)}${catParam(category)}${currencyParam(currency)}`),
  getByCategory: (start: string, end: string, accounts?: Set<string> | null, coreExpensesOnly?: boolean, includeRent = true, currency?: string | null) => fetchJson<CategoryData[]>(`/analytics/by-category?start_date=${start}&end_date=${end}${sourceParam(accounts)}${coreExpensesParam(coreExpensesOnly, includeRent)}${currencyParam(currency)}`),
  getByMerchant: (start: string, end: string, accounts?: Set<string> | null, category?: string | null, coreExpensesOnly?: boolean, includeRent = true, currency?: string | null) => fetchJson<MerchantData[]>(`/analytics/by-merchant?start_date=${start}&end_date=${end}${sourceParam(accounts)}${catParam(category)}${coreExpensesParam(coreExpensesOnly, includeRent)}${currencyParam(currency)}`),
  getTrends: (start: string, end: string, granularity: string | null, accounts?: Set<string> | null, category?: string | null, coreExpensesOnly?: boolean, includeRent = true, currency?: string | null) => fetchJson<TrendData[]>(`/analytics/trends?start_date=${start}&end_date=${end}&granularity=${granularity || 'monthly'}${sourceParam(accounts)}${catParam(category)}${coreExpensesParam(coreExpensesOnly, includeRent)}${currencyParam(currency)}`),
  getSubscriptions: (currency: string) => fetchJson<SubscriptionData[]>(`/analytics/subscriptions?currency=${encodeURIComponent(currency)}`),
  getRecurringCatalog: (start: string, end: string, currency: string) => fetchJson<RecurringCatalogResponse>(`/recurring/catalog?start_date=${start}&end_date=${end}${currencyParamRequired(currency)}`),
  getRecurringTrends: (start: string, end: string, metric: string, groupBy: string, currency: string) => fetchJson<RecurringTrendsResponse>(`/recurring/trends?start_date=${start}&end_date=${end}&metric=${metric}&group_by=${groupBy}${currencyParamRequired(currency)}`),
  getRecurringDetail: (id: string, currency: string) => fetchJson<RecurringDetail>(`/recurring/${id}?currency=${encodeURIComponent(currency)}`),
  getNetWorthTrackedHistory: (start: string, end: string, currency: string) => fetchJson<NetWorthTrackedHistory>(`/analytics/net-worth/tracked-history?start_date=${start}&end_date=${end}&currency=${encodeURIComponent(currency)}`),
  getSidebarAccounts: (currency: string) => fetchJson<{ accounts: SidebarAccount[]; exchange_rates: Array<{ from_currency: string; to: string; rate: number }> }>(`/sync/sidebar-accounts?currency=${encodeURIComponent(currency)}`),
  getTransactions: (params: Record<string, string>) => {
    const qs = new URLSearchParams(params).toString()
    return fetchJson<{ transactions: Transaction[]; total: number }>(`/transactions/?${qs}`)
  },
  getTransactionCategoryOptions: () => fetchJson<{ categories: TransactionCategoryOption[] }>(`/transactions/category-options`),
  getUncategorizedReview: (search = '', page = 1, limit = 100, currency = 'USD') =>
    fetchJson<UncategorizedReviewResponse>(
      `/transactions/uncategorized/review?${new URLSearchParams({
        ...(search ? { search } : {}),
        page: String(page),
        limit: String(limit),
        currency,
      }).toString()}`,
    ),
  applyUncategorizedReview: (data: UncategorizedApplyRequest) =>
    request<UncategorizedApplyResult>(`/transactions/uncategorized/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  undoUncategorizedReview: (previousValues: UncategorizedApplyResult['undo_payload']) =>
    request<UncategorizedUndoResult>(`/transactions/uncategorized/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ previous_values: previousValues }),
    }),
  updateTransaction: (id: string, data: Partial<Pick<Transaction, 'category' | 'merchant_clean' | 'is_recurring' | 'tags' | 'notes'>>, currency = 'USD') =>
    request<Transaction>(`/transactions/${id}?currency=${encodeURIComponent(currency)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  // Plaid
  getLinkToken: (products?: string, accountId?: string) => request<{ link_token?: string }>(`/sync/plaid/link-token?${new URLSearchParams({ ...(products ? { products } : {}), ...(accountId ? { account_id: accountId } : {}) }).toString()}`, { method: 'POST' }),
  exchangeToken: (public_token: string, institution_name = "", institutionId?: string, accountFingerprints?: Array<Record<string, string>>, accountId?: string, products?: string) =>
    request(`/sync/plaid/exchange?${new URLSearchParams({ public_token, institution_name, ...(institutionId ? { institution_id: institutionId } : {}), ...(accountFingerprints?.length ? { account_fingerprints: JSON.stringify(accountFingerprints) } : {}), ...(accountId ? { account_id: accountId } : {}), ...(products ? { products } : {}) }).toString()}`, { method: 'POST' }),
  syncPlaid: () => request<{ status: string; errors?: Array<{ source: string; stage: string; display_message?: string; message?: string }> }>(`/sync/plaid/sync`, { method: 'POST' }),
  getSyncStatus: () => fetchJson<{ accounts: Array<{ source: string; status: string; last_sync: string | null }> }>(`/sync/status`),
  // Imports
  previewImport: (file: File, source: string, kind: 'csv' | 'statement_pdf') => {
    const form = new FormData()
    form.append('file', file)
    form.append('source', source)
    form.append('kind', kind)
    return request<ImportPreview>(`/imports/preview`, { method: 'POST', body: form })
  },
  getImportPreview: (importId: string) => fetchJson<ImportPreview>(`/imports/${importId}`),
  commitImport: (importId: string) => request<ImportCommitResult>(`/imports/${importId}/commit`, { method: 'POST' }),
  // CSV
  uploadCsv: (file: File, source: string) => {
    const form = new FormData()
    form.append('file', file)
    form.append('source', source)
    return request(`/sync/csv`, { method: 'POST', body: form })
  },
  // Settings
  getSettings: (options?: { fresh?: boolean }) => {
    if (options?.fresh) invalidateCache('settings')
    return cachedRequest(
      'settings',
      () => fetchJson<SettingsResponse>(`/settings/`),
    )
  },
  disconnectAccount: (id: string) => request(`/settings/accounts/${id}`, { method: 'DELETE' }),
  clearTransactions: (source?: string) => request(`/settings/transactions${source ? `?source=${source}` : ''}`, { method: 'DELETE' }),
  signoutPreflight: () => fetchJson<{ needs_backup: boolean; transaction_count: number; last_backup_at: string | null }>(`/settings/signout/preflight`),
  signOut: () => request<{ status: string }>(`/settings/signout`, { method: 'POST' }).finally(() => {
    invalidateCache('settings')
    invalidateCache('vault-discover')
    invalidateCache('vault-backups')
  }),
  startGoogleDriveConnect: () => `${API_BASE}/settings/vault/google/start`,
  completeGoogleDriveConnect: (code: string, state: string) =>
    request<string>(`/settings/vault/google/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`),
  backupVaultToGoogleDrive: () => request<{ status: string; provider: string; vault_id: string; backup_created_at: string; backup_name: string; file_id: string | null }>(`/settings/vault/google/backup`, { method: 'POST' }).finally(() => {
    invalidateCache('settings')
    invalidateCache('vault-discover')
    invalidateCache('vault-backups')
  }),
  startGoogleDriveBackupJob: () => request<VaultBackupJob>(`/settings/vault/google/backup/start`, { method: 'POST' }).finally(() => {
    invalidateCache('settings')
    invalidateCache('vault-discover')
    invalidateCache('vault-backups')
  }),
  getGoogleDriveBackupJob: (jobId: string) => fetchJson<VaultBackupJob>(`/settings/vault/google/backup/jobs/${encodeURIComponent(jobId)}`),
  discoverGoogleVaults: (options?: { fresh?: boolean }) => {
    if (options?.fresh) invalidateCache('vault-discover')
    return cachedRequest(
      'vault-discover',
      () => fetchJson<{ vaults: VaultDiscoveryRow[] }>(`/settings/vault/google/discover`),
    )
  },
  listGoogleBackups: (vaultId?: string, options?: { fresh?: boolean }) => {
    const key = `vault-backups:${vaultId || 'default'}`
    if (options?.fresh) invalidateCache(key)
    return cachedRequest(
      key,
      () => fetchJson<{ vault_id: string; backups: VaultBackupRow[] }>(`/settings/vault/google/backups${vaultId ? `?vault_id=${encodeURIComponent(vaultId)}` : ''}`),
    )
  },
  startRestore: (vaultId?: string) => request<VaultRestoreJob>(`/settings/vault/google/restore/latest${vaultId ? `?vault_id=${encodeURIComponent(vaultId)}` : ''}`, { method: 'POST' }),
  getRestoreJob: (jobId: string) => fetchJson<VaultRestoreJob>(`/settings/vault/google/restore/jobs/${encodeURIComponent(jobId)}`),
  prefetchVaultPanel: async () => {
    const settings = await api.getSettings()
    if (!settings.vault.connected) return
    const localVaultId = settings.vault.vault_id
    if (localVaultId) {
      void api.listGoogleBackups(localVaultId).catch(() => undefined)
    }
    void api.discoverGoogleVaults().catch(() => undefined)
  },
  invalidateVaultPanelCache: () => {
    invalidateCache('settings')
    invalidateCache('vault-discover')
    invalidateCache('vault-backups')
  },
  // Projects
  getProjects: (currency: string, status?: string) => fetchJson<ProjectSummary[]>(`/projects/?currency=${encodeURIComponent(currency)}${status ? `&status=${encodeURIComponent(status)}` : ''}`),
  getProject: (id: string, currency: string) => fetchJson<ProjectDetail>(`/projects/${id}?currency=${encodeURIComponent(currency)}`),
  createProject: (data: Partial<ProjectSummary>) => request<ProjectSummary>(`/projects/`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  updateProject: (id: string, data: Partial<ProjectSummary>) => request<ProjectSummary>(`/projects/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  deleteProject: (id: string) => request<{ ok: true }>(`/projects/${id}`, { method: 'DELETE' }),
  addToProject: (projectId: string, txnIds: string[]) => request<{ added: number }>(`/projects/${projectId}/transactions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ transaction_ids: txnIds }) }),
  removeFromProject: (projectId: string, txnId: string) => request<{ ok: true }>(`/projects/${projectId}/transactions/${txnId}`, { method: 'DELETE' }),
  updateTransactionProject: (projectId: string, txnId: string, data: { description?: string }) => request<{ ok: true }>(`/projects/${projectId}/transactions/${txnId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  updateTransactionSplits: (projectId: string, txnId: string, contactIds: string[], shareAmounts?: Record<string, number> | null) => request<{ ok: true }>(`/projects/${projectId}/transactions/${txnId}/splits`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ contact_ids: contactIds, ...(shareAmounts ? { share_amounts: shareAmounts } : {}) }) }),
  // Project members
  addProjectMembers: (projectId: string, contactIds: string[]) => request<{ added: number }>(`/projects/${projectId}/members`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ contact_ids: contactIds }) }),
  removeProjectMember: (projectId: string, contactId: string) => request<{ ok: true }>(`/projects/${projectId}/members/${contactId}`, { method: 'DELETE' }),
  // Splitwise (optional integration)
  getSplitwiseStatus: () => fetchJson<SplitwiseStatus>(`/splitwise/status`),
  startSplitwiseConnect: () => `${API_BASE}/splitwise/start`,
  saveSplitwiseCredentials: (body: { client_id?: string; client_secret?: string; redirect_uri?: string; api_key?: string }) =>
    request<SplitwiseCredentialsInfo>(`/splitwise/credentials`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  clearSplitwiseCredentials: () => request<SplitwiseCredentialsInfo>(`/splitwise/credentials`, { method: 'DELETE' }),
  disconnectSplitwise: () => request<{ ok: true }>(`/splitwise/disconnect`, { method: 'POST' }),
  getSplitwiseFriends: () => fetchJson<{ friends: SplitwiseFriend[] }>(`/splitwise/friends`),
  getSplitwiseLinks: () => fetchJson<{ contacts: SplitwiseContactLink[] }>(`/splitwise/links`),
  linkSplitwiseContact: (contactId: string, splitwiseUserId: string, displayName?: string) =>
    request<{ ok: true }>(`/splitwise/links/${contactId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ splitwise_user_id: splitwiseUserId, display_name: displayName }) }),
  unlinkSplitwiseContact: (contactId: string) => request<{ ok: true }>(`/splitwise/links/${contactId}`, { method: 'DELETE' }),
  setSelfContact: (contactId: string) => request<{ ok: true }>(`/splitwise/self/${contactId}`, { method: 'PUT' }),
  previewSplitwiseCommit: (projectId: string) => fetchJson<SplitwiseCommitSummary>(`/splitwise/projects/${projectId}/preview`),
  commitSplitwiseProject: (projectId: string, amend = false) =>
    request<SplitwiseCommitSummary>(`/splitwise/projects/${projectId}/commit?amend=${amend}`, { method: 'POST' }),
  resetSplitwiseProject: (projectId: string) => request<{ ok: true; cleared: number }>(`/splitwise/projects/${projectId}/reset`, { method: 'POST' }),
  // Contacts
  getContacts: () => fetchJson<Contact[]>(`/contacts/`),
  createContact: (name: string, color?: string) => request<Contact>(`/contacts/`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, color }) }),
  updateContact: (id: string, data: { name?: string; color?: string }) => request<Contact>(`/contacts/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  getContactUsage: (id: string) => fetchJson<{ split_count: number; project_count: number }>(`/contacts/${id}/usage`),
  deleteContact: (id: string) => request<{ ok: true }>(`/contacts/${id}`, { method: 'DELETE' }),
}
