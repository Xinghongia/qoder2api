/**
 * 网关 API 客户端（唯一出口）。
 *
 * 认证语义与旧看板 dashboard.html 完全一致：
 *   · API Key：`?key=` 引导 → localStorage['wb-proxy-api-key']，请求带
 *     `Authorization: Bearer`；
 *   · 面板会话：`X-Panel-Token`，存 sessionStorage['wb-proxy-panel-token']；
 *   · **401 = 会话无效 → 弹面板登录框（不整页跳转）；403 = 服务端有意的拒绝，
 *     会话是好的 → 保留会话、把服务端的说明原样显示**（上游 v1.2.9）。
 *     此前把 403 也当 401，导致「默认密码下读取明文 Key」这种正常拒绝反而
 *     弹出登录框、还吞掉了服务端的原因说明。
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

/** 订阅「需要面板登录」事件（仅 401 触发）。 */
export function onUnauthorized(fn: () => void): () => void {
  unauthorizedHandlers.add(fn);
  return () => unauthorizedHandlers.delete(fn);
}

function fireUnauthorized() {
  unauthorizedHandlers.forEach((fn) => fn());
}

/**
 * 鉴权状态分流：只有 401（会话无效/缺失）才该弹登录框。
 * 403 是服务端**有意的拒绝**（如「面板仍是默认密码，不交明文 Key」），
 * 会话本身是好的——弹登录框只会让用户反复登录还看不到原因。
 */
function authStatusIs401(status: number): boolean {
  if (status !== 401) return false;
  fireUnauthorized();
  return true;
}

/** 解析非 2xx 响应体里的服务端说明（读一次 body，绝不重复读）。 */
function errorFromBody(status: number, text: string): ApiError {
  let parsed: any = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = {raw: text};
  }
  const msg = parsed?.error?.message || parsed?.message || `HTTP ${status}`;
  return new ApiError(status, msg, parsed?.error?.detail || parsed?.detail || '');
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
  const needLogin = authStatusIs401(r.status);
  const text = await r.text();
  if (needLogin) throw new ApiError(401, '需要面板登录');
  if (!r.ok) throw errorFromBody(r.status, text);
  let parsed: any = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = {raw: text};
  }
  return parsed as T;
}

/**
 * GET 原始文本（不解析 JSON）。
 *
 * 账号导出等下载场景必须把服务端返回的字节原样落盘；鉴权语义与 request
 * 一致（401 → 登录框；403 → 保留会话并把服务端说明原样抛出），非 2xx 仍抛
 * ApiError（404 的 error.message 原样保留，交给调用方展示）。
 */
async function getText(path: string): Promise<string> {
  const r = await fetch(path, {
    method: 'GET',
    cache: 'no-store',
    headers: authHeaders(),
  });
  const needLogin = authStatusIs401(r.status);
  const text = await r.text();
  if (needLogin) throw new ApiError(401, '需要面板登录');
  if (!r.ok) throw errorFromBody(r.status, text);
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
    // 登录页发 {username, password}；后端对旧客户端（只发 password）保持兼容
    login: (username: string, password: string) =>
      http.post<{token: string; using_default_password?: boolean}>('/panel/login', {
        username,
        password,
      }),
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
    /** 每账号「本轮已签到」状态（以活动平台为准，单独请求避免拖慢账号表首屏）。 */
    checkinState: (realm?: string) =>
      http.get(`/accounts/checkin-state${realm ? `?realm=${realm}` : ''}`),
    // 额度快照刷新：POST /accounts/credits 支持 {uid} 单账号 / {} 全部账号
    // （GET 版本忽略 realm 且无法指定 uid，与后端不一致，故改为 POST）。
    // ttlSeconds：快照比它新时服务端直接返回缓存（cached=true）——进入页面
    // 的自动刷新用它，避免反复打上游；手动刷新不传 = 始终回源。
    credits: (uid?: string, ttlSeconds?: number) =>
      http.post('/accounts/credits', {
        ...(uid ? {uid} : {}),
        ...(ttlSeconds != null ? {ttl: ttlSeconds} : {}),
      }),
    checkin: (uid?: string) => http.post('/accounts/checkin', uid ? {uid} : {}),
    refresh: (uid?: string) => http.post('/accounts/refresh', uid ? {uid} : {}),
    // 连通测试：默认测 Qwen3.8-Flash（免费模型、0 积分）——额度耗尽的账号
    // 也能用它验证"这个号还能不能用"；传 model 可测其它模型。
    test: (uid: string, model?: string) =>
      http.post('/accounts/test', model ? {uid, model} : {uid}),
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
    /**
     * 分页请求记录。from/to 为本地日期（YYYY-MM-DD，含两端，可选）：
     * 用量页的「全部请求记录」跟随时间范围；不传 = 不限时间（最近 N 条）。
     */
    recent: (limit: number, page: number, realm?: string, from?: string, to?: string) => {
      const q = new URLSearchParams({limit: String(limit), page: String(page)});
      if (realm) q.set('realm', realm);
      if (from) q.set('from', from);
      if (to) q.set('to', to);
      return http.get(`/usage/recent?${q.toString()}`);
    },
    perf: (realm?: string) => http.get(`/usage/perf${realm ? `?realm=${realm}` : ''}`),
    byAccount: () => http.get('/usage/by-account'),
    analytics: (scope: 'today' | 'all') => http.get(`/usage/analytics?scope=${scope}`),
    /** 按天汇总（仪表盘趋势图）：{days: [{date, day, requests, tokens, failed, credit}]} */
    daily: (days: number = 14, realm?: string) =>
      http.get(`/usage/daily?days=${days}${realm ? `&realm=${realm}` : ''}`),
    /**
     * 用量统计页（/usage）：SQLite 聚合库 + 任意时间范围/模型/密钥维度。
     * range=today|7d|30d|custom（custom 需 from/to=YYYY-MM-DD）；
     * group=total|model|key_id（趋势分组）；granularity=auto|hour|day。
     */
    stats: (params: {
      range: 'today' | '7d' | '30d' | 'custom';
      from?: string;
      to?: string;
      realm?: string;
      group?: 'total' | 'model' | 'key_id';
      granularity?: 'auto' | 'hour' | 'day';
    }) => {
      const q = new URLSearchParams({range: params.range});
      if (params.from) q.set('from', params.from);
      if (params.to) q.set('to', params.to);
      if (params.realm) q.set('realm', params.realm);
      if (params.group) q.set('group', params.group);
      if (params.granularity && params.granularity !== 'auto') {
        q.set('granularity', params.granularity);
      }
      return http.get(`/usage/stats?${q.toString()}`);
    },
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
