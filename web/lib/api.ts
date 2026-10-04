/**
 * 网关 API 客户端（唯一出口）。
 *
 * 认证语义与旧看板 dashboard.html 完全一致：
 *   · API Key：`?key=` 引导 → localStorage['wb-proxy-api-key']，请求带
 *     `Authorization: Bearer`；
 *   · 面板会话：`X-Panel-Token`，存 sessionStorage['wb-proxy-panel-token']；
 *   · 401/403 → 交给上层的面板登录弹窗（不整页跳转）。
 */

export const KEY_STORE = 'wb-proxy-api-key';
export const PANEL_STORE = 'wb-proxy-panel-token';

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, message: string, detail = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

let apiKey = '';
let panelToken = '';

const unauthorizedHandlers = new Set<() => void>();

/** 订阅「需要面板登录」事件（401/403 时触发）。 */
export function onUnauthorized(fn: () => void): () => void {
  unauthorizedHandlers.add(fn);
  return () => unauthorizedHandlers.delete(fn);
}

function fireUnauthorized() {
  unauthorizedHandlers.forEach((fn) => fn());
}

/**
 * 非 ASCII 的 key 无法进入 HTTP 头（fetch 会直接抛
 * "String contains non ISO-8859-1 code point"），这种值不可能是真 key，
 * 直接丢弃，让面板会话接管这次请求。
 */
function usableKey(value: string): string {
  return value && /^[\x20-\x7e]+$/.test(value) ? value : '';
}

/** 启动时调用一次：`?key=` 优先，随即从地址栏抹掉。 */
export function initApiKey(): string {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('key');
    if (fromUrl) {
      apiKey = fromUrl.trim();
      localStorage.setItem(KEY_STORE, apiKey);
      window.history.replaceState(null, '', window.location.pathname);
    } else {
      apiKey = (localStorage.getItem(KEY_STORE) || '').trim();
    }
  } catch {
    apiKey = '';
  }
  try {
    panelToken = sessionStorage.getItem(PANEL_STORE) || '';
  } catch {
    panelToken = '';
  }
  return apiKey;
}

// 模块加载即读取凭据：React 的子组件 effect 先于父组件 effect 执行，
// 若等到 AuthProvider 的 effect 里才初始化，未被 authReady 门控的组件
// （模型库/调度条/福利中心等）会先发出**不带面板 token** 的请求 → 401 空数据。
if (typeof window !== 'undefined') initApiKey();

export function getApiKey(): string {
  return apiKey;
}

export function setApiKey(value: string) {
  apiKey = value.trim();
  try {
    if (apiKey) localStorage.setItem(KEY_STORE, apiKey);
    else localStorage.removeItem(KEY_STORE);
  } catch {
    /* 隐私模式下 localStorage 不可用，忽略 */
  }
}

export function getPanelToken(): string {
  return panelToken;
}

export function setPanelToken(value: string) {
  panelToken = value;
  try {
    if (value) sessionStorage.setItem(PANEL_STORE, value);
    else sessionStorage.removeItem(PANEL_STORE);
  } catch {
    /* 同上 */
  }
}

function authHeaders(base?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = {...(base || {})};
  const key = usableKey(apiKey);
  if (key) h['Authorization'] = `Bearer ${key}`;
  if (panelToken) h['X-Panel-Token'] = panelToken;
  return h;
}

async function request<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const init: RequestInit = {
    method,
    cache: 'no-store',
    headers: authHeaders(body === undefined ? undefined : {'Content-Type': 'application/json'}),
  };
  if (body !== undefined) init.body = JSON.stringify(body);

  const r = await fetch(path, init);
  if (r.status === 401 || r.status === 403) {
    fireUnauthorized();
    throw new ApiError(r.status, '需要面板登录');
  }
  const text = await r.text();
  let parsed: any = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = {raw: text};
  }
  if (!r.ok) {
    const msg = parsed?.error?.message || parsed?.message || `HTTP ${r.status}`;
    throw new ApiError(r.status, msg, parsed?.error?.detail || parsed?.detail || '');
  }
  return parsed as T;
}

/**
 * GET 原始文本（不解析 JSON）。
 *
 * 账号导出等下载场景必须把服务端返回的字节原样落盘；鉴权头与 401/403 →
 * onUnauthorized 的语义和 request 完全一致，非 2xx 仍抛 ApiError（404 的
 * error.message 原样保留，交给调用方展示）。
 */
async function getText(path: string): Promise<string> {
  const r = await fetch(path, {
    method: 'GET',
    cache: 'no-store',
    headers: authHeaders(),
  });
  if (r.status === 401 || r.status === 403) {
    fireUnauthorized();
    throw new ApiError(r.status, '需要面板登录');
  }
  const text = await r.text();
  if (!r.ok) {
    let parsed: any = {};
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      parsed = {raw: text};
    }
    const msg = parsed?.error?.message || parsed?.message || `HTTP ${r.status}`;
    throw new ApiError(r.status, msg, parsed?.error?.detail || parsed?.detail || '');
  }
  return text;
}

export const http = {
  get: <T = any>(path: string) => request<T>('GET', path),
  getText: (path: string) => getText(path),
  post: <T = any>(path: string, body?: unknown) => request<T>('POST', path, body),
};

/* ------------------------------------------------------------------ 端点 */

export type Realm = 'intl' | 'cn';

export interface PanelStatus {
  authenticated: boolean;
  panel_password_required?: boolean;
  panel_password_is_default?: boolean;
  [k: string]: unknown;
}

export const api = {
  panel: {
    status: () => http.get<PanelStatus>('/panel/status'),
    login: (password: string) => http.post<{token: string}>('/panel/login', {password}),
    logout: () => http.post('/panel/logout'),
    // 后端 /panel/password 收 {current, new}（见 qoder2api/api/panel_routes.py）
    password: (current: string, next: string) =>
      http.post<{ok: boolean; token: string}>('/panel/password', {current, new: next}),
  },
  realm: {
    get: () => http.get<{mode: string; preferred: string}>('/realm'),
    set: (mode: string, preferred?: string) => http.post('/realm', {mode, preferred}),
  },
  models: (realm?: Realm) => http.get(`/v1/models${realm ? `?realm=${realm}` : ''}`),
  accounts: {
    list: (realm: string = 'all') => http.get(`/accounts?realm=${realm}`),
    // 额度快照刷新：POST /accounts/credits 支持 {uid} 单账号 / {} 全部账号
    // （GET 版本忽略 realm 且无法指定 uid，与后端不一致，故改为 POST）。
    credits: (uid?: string) => http.post('/accounts/credits', uid ? {uid} : {}),
    checkin: (uid?: string) => http.post('/accounts/checkin', uid ? {uid} : {}),
    refresh: (uid?: string) => http.post('/accounts/refresh', uid ? {uid} : {}),
    test: (uid: string) => http.post('/accounts/test', {uid}),
    set: (uid: string, patch: Record<string, unknown>) => http.post('/accounts/set', {uid, ...patch}),
    setAll: (patch: Record<string, unknown>) => http.post('/accounts/set-all', patch),
    remove: (uid: string) => http.post('/accounts/delete', {uid}),
    importJSON: (payload: unknown) => http.post('/accounts/import', payload),
    importDesktop: (payload: unknown) => http.post('/accounts/import/desktop', payload),
    importPat: (payload: unknown) => http.post('/accounts/import/pat', payload),
    // 后端 start_login 返回 {state, authUrl, realm, platform}：授权链接字段是
    // authUrl（浏览器里选账号完成授权），没有独立的 user_code。
    loginStart: (realm: Realm) =>
      http.post<{state: string; authUrl: string; realm: string; platform: string}>(
        '/accounts/login/start',
        {realm},
      ),
    // 后端 poll_login 返回 {status: pending|ok|expired|unknown|error, message?, account?}
    loginPoll: (state: string) =>
      http.get<{status: string; message?: string; account?: {uid?: string; nickname?: string}}>(
        `/accounts/login/poll?state=${encodeURIComponent(state)}`,
      ),
    loginCancel: (state: string) => http.post('/accounts/login/cancel', {state}),
  },
  usage: {
    summary: (realm?: string) => http.get(`/usage${realm ? `?realm=${realm}` : ''}`),
    recent: (limit: number, page: number, realm?: string) =>
      http.get(`/usage/recent?limit=${limit}&page=${page}${realm ? `&realm=${realm}` : ''}`),
    perf: (realm?: string) => http.get(`/usage/perf${realm ? `?realm=${realm}` : ''}`),
    byAccount: () => http.get('/usage/by-account'),
    analytics: (scope: 'today' | 'all') => http.get(`/usage/analytics?scope=${scope}`),
  },
  tasks: () => http.get('/tasks'),
  tasksRun: (uid?: string) => http.post('/tasks/run', uid ? {uid} : {}),
  tasksTravel: (uid?: string) => http.post('/tasks/travel', uid ? {uid} : {}),
  scheduler: {
    get: () => http.get('/scheduler'),
    trigger: () => http.post('/scheduler/trigger'),
    toggle: (enabled: boolean) => http.post('/scheduler/toggle', {enabled}),
  },
  settings: {
    get: () => http.get('/settings'),
    save: (patch: Record<string, unknown>) => http.post('/settings/save', patch),
    reveal: (id: string) => http.get(`/settings/reveal?id=${encodeURIComponent(id)}`),
  },
  logs: (sinceId: number, limit: number) => http.get(`/logs?since_id=${sinceId}&limit=${limit}`),
  logsClear: () => http.post('/logs/clear'),
  diagVm: (force = false) => http.get(`/diag/vm${force ? '?force=1' : ''}`),
  updateCheck: () => http.get('/update/check'),
  identityExport: () => http.get('/identity/export'),
};
