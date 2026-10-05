'use client';

import * as React from 'react';
import {RefreshCw, Users, Zap} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {AddAccountBar} from '@/components/common/accounts/AddAccountBar';
import {
  AccountTable,
  type AccountRow,
  type CreditsMeta,
} from '@/components/common/gateway/AccountTable';
import {GrowthTasksPanel} from '@/components/common/gateway/GrowthTasksPanel';
import {api} from '@/lib/api';
import {useAddAccount} from '@/lib/add-account-context';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useRealm} from '@/lib/realm-context';

/**
 * `/accounts` —— 账号管理（从原「网关与运维」页拆出）。
 *
 * 加载策略对齐 workbuddy 管理面板（用户指定的体验基准）：
 *   · **三条链路各走各的**：账号列表（本地、快）先渲染；活动平台的「本轮已签到」
 *     状态（1-4 秒）后到后合并；额度刷新走服务端 TTL 在后台补。此前用
 *     Promise.all 把三者绑在一起，进页面必须等最慢的一条（最慢 4 秒），期间
 *     整张表是骨架屏——"一进页面就在转圈、什么都看不到"的根源。
 *   · **stale-while-revalidate**：模块级缓存 + 心跳刷新只在有数据后就地替换，
 *     绝不把已显示的表格清空重画；骨架屏只在"确实没有任何数据"时出现。
 *   · **心跳**：列表 30s、签到状态 10 分钟自动刷新（页面隐藏时跳过、切回立即补），
 *     状态不会停在打开页面那一刻。
 * 区域由右上角 RealmToggle 决定；所有操作原地生效，不切换视图。
 */

// 心跳节奏：列表 30s（冷却/失败计数这类运行时状态变化快）；签到状态 10 分钟
// （一轮只在每天 10:00 变一次，campaigns 每次都要真打上游活动平台，刷太勤
// 没有意义——签到动作后会立即单独补一次）。
const LIST_HEARTBEAT_MS = 30_000;
const ROUND_HEARTBEAT_MS = 600_000;
// 进入页面自动刷新额度的服务端 TTL：60 秒内重复进入直接命中快照，不再打上游。
const CREDITS_TTL_SECONDS = 60;

// 模块级缓存：SPA 内部来回切页时先用旧数据渲染（Next App Router 切页会卸载
// 组件，但模块状态还在），进账号页不再从骨架屏开始。
let cachedAccounts: AccountRow[] = [];
let cachedRound = new Map<string, RoundState>();
let cachedCreditsMeta: Record<string, CreditsMeta> = {};

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const clockNow = () => new Date().toLocaleTimeString('zh-CN', {hour12: false});

export default function AccountsPage() {
  const {view} = useRealm();
  const {refreshKey} = useAddAccount();
  const [accounts, setAccounts] = React.useState<AccountRow[]>(() => cachedAccounts);
  const [round, setRound] = React.useState<Map<string, RoundState>>(() => cachedRound);
  const [creditsMeta, setCreditsMeta] =
    React.useState<Record<string, CreditsMeta>>(() => cachedCreditsMeta);
  // 骨架屏的判据是"有没有数据"，不是"请求在不在飞"——后者会让每次心跳
  // 刷新和每次进页面都闪一遍骨架（workbuddy 的 asyncFlags 同款语义）。
  const [listLoading, setListLoading] = React.useState(cachedAccounts.length === 0);
  const [roundPending, setRoundPending] = React.useState(cachedRound.size === 0);
  const [loadError, setLoadError] = React.useState('');
  const [refreshing, setRefreshing] = React.useState(false); // 仅手动刷新时转圈
  const [updatedAt, setUpdatedAt] = React.useState('');
  const [busy, setBusy] = React.useState('');

  /** 账号列表（本地接口，快）：只替换数据，失败保留旧值并如实提示。 */
  const loadList = React.useCallback(async () => {
    try {
      const acc = await api.accounts.list('all');
      const list = (acc?.accounts as AccountRow[]) || [];
      cachedAccounts = list;
      setAccounts(list);
      setUpdatedAt(clockNow());
      setLoadError('');
    } catch (e) {
      // 已有数据时不清空（继续显示上一次成功的列表），首次加载失败才给错误态
      if (cachedAccounts.length === 0) setLoadError(errText(e));
      else notify.err('账号列表刷新失败', errText(e));
    } finally {
      setListLoading(false);
    }
  }, []);

  /** 上游活动平台的「本轮已签到」状态（慢）：后到后合并，失败保持旧值。 */
  const loadRound = React.useCallback(async () => {
    try {
      const r = (await api.accounts.checkinState()) as {accounts?: RoundState[]};
      const m = new Map<string, RoundState>();
      for (const s of r?.accounts || []) {
        if (s?.uid) m.set(s.uid, s);
      }
      cachedRound = m;
      setRound(m);
    } catch {
      // 静默：徽标按"未知"回落本地判断，不打扰用户
    } finally {
      setRoundPending(false);
    }
  }, []);

  /** 额度刷新（服务端 TTL）：进入页面自动跑一次，命中快照则秒回、不打上游。 */
  const loadCredits = React.useCallback(async () => {
    try {
      const r = (await api.accounts.credits(undefined, CREDITS_TTL_SECONDS)) as {
        results?: CreditsResult[];
        accounts?: AccountRow[];
      };
      const meta: Record<string, CreditsMeta> = {};
      for (const x of r?.results || []) {
        if (x?.uid) {
          meta[x.uid] = {
            ok: !!x.ok,
            cached: !!x.cached,
            age: x.age ?? 0,
            error: x.error || undefined,
          };
        }
      }
      cachedCreditsMeta = meta;
      setCreditsMeta(meta);
      // 服务端顺带返回刷新后的账号视图，直接换上去（额度/套餐是最新的）
      if (r?.accounts?.length) {
        cachedAccounts = r.accounts;
        setAccounts(r.accounts);
        setUpdatedAt(clockNow());
      }
    } catch {
      // 静默：列表已有上游快照值，界面会标注来源，不打断
    }
  }, []);

  useAuthedLoad(() => {
    void loadList();
    void loadRound();
    void loadCredits();
  }, [refreshKey]);

  // 心跳：列表 30s、签到状态 3 分钟（隐藏标签页跳过，切回立即补一次）
  useHeartbeat(() => void loadList(), LIST_HEARTBEAT_MS);
  useHeartbeat(() => void loadRound(), ROUND_HEARTBEAT_MS);

  /** 手动刷新（头部按钮）：三个链路一起拉，只有这时按钮才转圈。 */
  const refreshAll = React.useCallback(async () => {
    setRefreshing(true);
    try {
      await Promise.all([loadList(), loadRound(), loadCredits()]);
    } finally {
      setRefreshing(false);
    }
  }, [loadList, loadRound, loadCredits]);

  const rows = React.useMemo(
    () =>
      accounts
        .filter((a) => a.realm === view)
        .map((a) => {
          const r = round.get(a.uid);
          return r
            ? {...a, roundClaimed: r.claimed === true, roundNote: r.round_note}
            : a;
        }),
    [accounts, round, view],
  );
  const realmLabel = view === 'intl' ? '国际版' : '国内版';

  const onCheckin = async (uid?: string) => {
    setBusy(uid || 'all');
    try {
      const r = (await api.accounts.checkin(uid)) as {results?: CheckinResult[]};
      const results = r?.results || [];
      // 反馈口径照旧看板：逐个账号报「成功 / 失败原因」，不能只回一句
      // 「已提交」——失败（同人去重、上游拒绝、无接口）必须如实说出来。
      // 且「成功」只用在**真到账**上：已领取 / 暂无可领 走中性提示，否则
      // 一次正确地没发积分的签到会被读成"积分丢了"（上游 issue #20）。
      if (!results.length) {
        notify.warn(uid ? '未找到该账号' : '未发现可签到账号', '账号可能已停用或凭证缺失');
      } else if (uid || results.length === 1) {
        const row = checkinOutcome(results[0]);
        if (row.state === 'claimed') notify.ok('签到完成', row.text);
        else if (row.state === 'failed') notify.err('签到失败', row.text);
        else notify.warn('本轮无可领', row.text);
      } else {
        const rows = results.map(checkinOutcome);
        const claimed = rows.filter((x) => x.state === 'claimed');
        const failed = rows.filter((x) => x.state === 'failed');
        const lines =
          rows.length <= 3
            ? rows.map((x) => x.text).join('；')
            : `${rows.map((x) => x.name).join('、')}；明细 ${rows[0].text}`;
        if (claimed.length) {
          notify.ok(
            `每日签到：${claimed.length}/${rows.length} 到账`,
            claimed.map((x) => x.text).join('；'),
          );
        } else if (failed.length === rows.length) {
          notify.err('每日签到：全部失败', lines);
        } else {
          notify.warn(
            `每日签到：${rows.length - failed.length}/${rows.length} 个没到账（已领取或暂无可领）`,
            lines,
          );
        }
      }
      await loadList();
      void loadRound(); // 签到后本轮状态必然变化，单独补一次
    } catch (e) {
      notify.err('签到失败', errText(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="账号"
        description={`${realmLabel}账号池 · 列表 30 秒自动刷新，所有操作原地生效${
          updatedAt ? ` · 更新于 ${updatedAt}` : ''
        }`}
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void refreshAll()}
              disabled={refreshing}
            >
              <RefreshCw className={refreshing ? 'size-4 animate-spin' : 'size-4'} />
              刷新
            </Button>
            <Button size="sm" onClick={() => void onCheckin()} disabled={busy === 'all'}>
              <Zap className="size-4" />
              全部签到
            </Button>
          </>
        }
      />

      <AddAccountBar />

      {listLoading && accounts.length === 0 ? (
        <Skeleton className="h-64 w-full rounded-[20px]" />
      ) : loadError && accounts.length === 0 ? (
        <EmptyState
          icon={Users}
          title="账号列表加载失败"
          description={`${loadError}。点右上角「刷新」重试，或确认网关进程正常。`}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Users}
          title={`暂无${realmLabel}账号`}
          description="用「扫描本机凭证」或 OAuth / PAT 导入账号后即可开始转发。"
        />
      ) : (
        <AccountTable
          rows={rows}
          busy={busy}
          creditsMeta={creditsMeta}
          roundPending={roundPending}
          onCheckin={onCheckin}
          onChanged={loadList}
        />
      )}

      <GrowthTasksPanel realm={view} onChanged={loadList} />
    </div>
  );
}

/** 后端 /accounts/checkin-state 的单账号「本轮」状态（qoder2api/tasks.py）。 */
interface RoundState {
  uid?: string;
  claimed?: boolean;
  claimable?: boolean;
  round_note?: string;
}

/** 后端 /accounts/credits 的单账号结果（qoder2api/accounts.py refresh_credits）。 */
interface CreditsResult {
  uid?: string;
  ok?: boolean;
  cached?: boolean;
  age?: number;
  credits?: AccountRow['credits'];
  plan?: string;
  error?: string;
}

/** 后端 /accounts/checkin 的单账号结果（qoder2api/api/accounts_routes.py）。 */
interface CheckinResult {
  uid?: string;
  nickname?: string;
  /** 只代表「这次请求没报错」；是否真到账要看 claimed / earned_credit */
  ok?: boolean;
  earned_credit?: number;
  /** logs 的最后一行（往往是「当前额度余额」这类收尾行，不是结论） */
  msg?: string;
  logs?: string[];
  /** 活动平台结论文本（旧 sash 兜底路径没有这个字段） */
  message?: string;
  /** 本轮**真实新增**的活动名；空数组 = 没领到（已领取/暂无可领） */
  claimed?: string[];
  /** 下一个「每日 10:00（UTC+8）」窗口 */
  next_available_at?: number;
  next_available_note?: string;
}

/**
 * 下次可签到时间：优先用后端格式化好的 UTC+8 note；缺失时按 epoch 秒兜底换算，
 * 读数一律走 getUTC*，不受浏览器本地时区影响。
 */
function fmtNextCheckin(x: CheckinResult): string {
  if (x.next_available_note) return String(x.next_available_note);
  const sec = Number(x.next_available_at || 0);
  if (!sec) return '';
  const d = new Date(sec * 1000 + 8 * 3600 * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}（UTC+8）`;
}

/**
 * 签到结论：三态分开渲染，绝不把「已领取」「暂无可领」写成「签到成功」。
 * 结论文案优先级：活动平台 message → 日志结论行 → 旧 msg（余额行不当结论）→ 兜底。
 */
function checkinOutcome(x: CheckinResult): {
  state: 'claimed' | 'idle' | 'failed';
  name: string;
  text: string;
} {
  const name = x.nickname || (x.uid || '').slice(0, 6) || '账号';
  const marked = (x.logs || []).find((l) => /[✓⚠!—]/.test(l));
  const fromLog = (marked || '')
    .replace(/^[✓⚠!\-\s]*\[[^\]]*\]\s*/, '')
    .trim();
  const legacyMsg =
    x.msg && !/当前额度余额/.test(String(x.msg)) ? String(x.msg).trim() : '';
  const detail = x.message || fromLog || legacyMsg;
  if (x.ok === false) {
    return {state: 'failed', name, text: `${name}：${detail || '失败'}`};
  }
  const claimed = (Array.isArray(x.claimed) ? x.claimed : []).filter(
    (v) => typeof v === 'string' && v,
  );
  const earned = Number(x.earned_credit || 0);
  if (claimed.length || earned > 0) {
    const text =
      detail ||
      `领取成功${earned > 0 ? ` +${earned} 积分` : ''}${
        claimed.length ? `（${claimed.join('、')}）` : ''
      }`;
    return {state: 'claimed', name, text: `${name}：${text}`};
  }
  const next = fmtNextCheckin(x);
  return {
    state: 'idle',
    name,
    text:
      `${name}：${detail || '本次没有新增积分（已领取或暂无可领取的活动）'}` +
      (next ? `；下次可签到 ${next}` : ''),
  };
}
