'use client';

import * as React from 'react';
import {
  CalendarDays,
  ExternalLink,
  Gift,
  Loader2,
  Package,
  RefreshCw,
  Wallet,
} from 'lucide-react';

import {EmptyState} from '@/components/common/layout/EmptyState';
import {StatCard, type StatTone} from '@/components/common/layout/StatCard';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Skeleton} from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {ApiError, api, http, type Realm} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {cn} from '@/lib/utils';

import {RewardCodesPanel, type RewardCode} from './RewardCodesPanel';
import {TaskLogBox} from './TaskLogBox';
import {VmStatusCard} from './VmStatusCard';

/**
 * 签到与福利中心（「网关与运维」页）。
 *
 * 行为基准是旧看板 dashboard.html 的 #sectionGrowthTasks：
 *   · 两个按钮的文案固定为「领取全部福利」「仅领 Pro 福利包」；
 *   · 「领取全部福利」依次调 POST /tasks/run（活动全量）与 POST /tasks/travel
 *     （Pro 福利包），每个账号的成功/失败数量如实汇报（按钮文案已按回归要求固定）；
 *   · uid=all 时按活动聚合：任务行展示 accounts_by_state 各状态徽章
 *     （只读活动标「仅查看（无奖励）」，不标成可领）；
 *   · 券类/兑换码奖励显示 reward_text（如「兑换码 ×1」）而不是 +N 积分；
 *   · 已领取的兑换码单独成表（RewardCodesPanel：复制 + 活动页/二维码）；
 *   · 本机虚拟化检测（VmStatusCard，GET /diag/vm）。
 */

/* ------------------------------------------------------------------ 类型 */

interface AccountOption {
  uid: string;
  nickname?: string;
  realm?: string;
}

interface TaskRow {
  task_code?: string;
  name?: string;
  description?: string;
  jump_url?: string;
  status?: string;
  current?: number;
  target?: number;
  reward_credit?: number;
  reward_energy?: number;
  /** 券/兑换码类奖励的展示文案（后端 tasks.py 下发）。 */
  reward_text?: string;
  /** uid=all 的聚合行：状态 → 账号名列表。 */
  accounts_by_state?: Record<string, string[]>;
}

interface TaskTravel {
  state?: string;
  reward_credit?: number;
  daily_limit_reached?: boolean;
}

interface EnergyRow {
  uid?: string;
  nickname?: string;
  realm?: string;
  remain?: number;
}

interface TaskSummary {
  mode?: string;
  energy?: number;
  energy_breakdown?: EnergyRow[];
  streak_days?: number;
  travel?: TaskTravel;
  plan?: string;
  codes?: RewardCode[];
  credits?: {remain?: number};
  accounts_total?: number;
}

interface TasksView {
  tasks?: TaskRow[];
  summary?: TaskSummary;
  accounts?: AccountOption[];
  mode?: string;
  msg?: string;
}

interface RunResp {
  ok?: boolean;
  credit_added?: number;
  logs?: string[];
  accounts_count?: number;
  msg?: string;
}

interface TravelResult {
  uid?: string;
  nickname?: string;
  action?: string;
  msg?: string;
  reward_credit?: number;
}

interface TravelResp {
  ok?: boolean;
  results?: TravelResult[];
  msg?: string;
  logs?: string[];
  accounts_count?: number;
}

/* -------------------------------------------------------------- 小工具 */

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const uid8 = (uid: string) => (uid || '').slice(0, 8);
const fmt = (n: number | null | undefined) => (n ?? 0).toLocaleString('en-US');

const TONE_BADGE = {
  ok: 'border-emerald-600/30 bg-emerald-600/10 text-emerald-700 dark:text-emerald-400',
  warning: 'border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  danger: 'border-red-500/35 bg-red-500/10 text-red-700 dark:text-red-400',
  info: 'border-sky-600/30 bg-sky-600/10 text-sky-700 dark:text-sky-400',
  muted: 'border-border/60 text-muted-foreground',
} as const;

/** 聚合状态的展示顺序（与后端 aggregate_campaign_rows 的 ORDER 一致）。 */
const STATE_ORDER = [
  'claimable',
  'claimed',
  'out_of_stock',
  'task_required',
  'risk_blocked',
  'inactive',
  'view_only',
  'ineligible',
  'no_eligibility',
];

const STATE_LABEL: Record<string, string> = {
  claimable: '可领',
  claimed: '已领',
  out_of_stock: '名额发完',
  task_required: '需先完成任务',
  risk_blocked: '风控拦截',
  inactive: '活动已结束',
  // 只读类活动（action_type != CLAIM_BENEFIT）：没有奖励可领，不能标成「可领」
  view_only: '仅查看（无奖励）',
  ineligible: '暂不可领',
  no_eligibility: '无资格(不在定向)',
};

const STATE_BADGE: Record<string, string> = {
  claimable: TONE_BADGE.ok,
  claimed: TONE_BADGE.muted,
  out_of_stock: TONE_BADGE.warning,
  task_required: TONE_BADGE.warning,
  risk_blocked: TONE_BADGE.danger,
  inactive: TONE_BADGE.muted,
  view_only: TONE_BADGE.info,
  ineligible: TONE_BADGE.muted,
  no_eligibility: TONE_BADGE.muted,
};

function statusBadge(t: TaskRow): {text: string; className: string} {
  const done = (t.current ?? 0) >= (t.target ?? 1);
  if (t.status === 'claimed') return {text: '已领取', className: TONE_BADGE.muted};
  if (t.status === 'completed' || done) return {text: '待领奖', className: TONE_BADGE.ok};
  if (t.status === 'accepted' || t.status === 'in_progress') {
    return {text: '进行中', className: TONE_BADGE.info};
  }
  if (t.status === 'not_accepted') return {text: '未接取', className: TONE_BADGE.muted};
  return {text: t.status || '-', className: TONE_BADGE.muted};
}

function StateBadges({by}: {by: Record<string, string[]>}) {
  const keys = STATE_ORDER.filter((k) => (by[k] || []).length > 0);
  const extra = Object.keys(by).filter((k) => !STATE_ORDER.includes(k) && by[k]?.length);
  return (
    <div className="flex max-w-[300px] flex-wrap gap-1">
      {[...keys, ...extra].map((k) => (
        <Badge
          key={k}
          variant="outline"
          title={(by[k] || []).join('、')}
          className={cn('rounded-full text-[10px]', STATE_BADGE[k] || TONE_BADGE.muted)}
        >
          {STATE_LABEL[k] || k} {by[k].length}
        </Badge>
      ))}
    </div>
  );
}

/** 逐账号解析 run_batch_checkin 的日志块，得到成功/失败/同人已领数量。 */
interface AccountRunBlock {
  name: string;
  failed: boolean;
  blocked: boolean;
  error: string;
}

function parseRunLogs(logs: string[]): AccountRunBlock[] {
  const blocks: AccountRunBlock[] = [];
  let cur: AccountRunBlock | null = null;
  const head = /^={4,}\s*正在为账号\s*\[(.+?)\]\s*执行每日签到/;
  for (const raw of logs) {
    const line = raw.trim();
    const m = head.exec(line);
    if (m) {
      if (cur) blocks.push(cur);
      cur = {name: m[1], failed: false, blocked: false, error: ''};
      continue;
    }
    if (!cur) continue;
    if (line.startsWith('! [')) {
      cur.failed = true;
      if (!cur.error) cur.error = line.replace(/^!\s*/, '');
    } else if (line.startsWith('⚠ [')) {
      cur.blocked = true;
    }
  }
  if (cur) blocks.push(cur);
  return blocks;
}

const shortName = (name: string) => name.replace(/\s*\([^)]*\)\s*$/, '');

/** 活动全量（/tasks/run）结果汇报：数量如实，不夸大。 */
function reportRun(r: RunResp) {
  const logs = r.logs || [];
  if (!logs.length) {
    if (r.msg) notify.err('未能领取活动福利', r.msg);
    else notify.warn('活动全量：没有返回日志', '请稍后重试或点「刷新」');
    return;
  }
  const blocks = parseRunLogs(logs);
  if (!blocks.length) {
    notify.warn('活动全量：未识别到账号日志', r.msg || '请展开任务日志查看逐行输出');
    return;
  }
  const total = blocks.length;
  const failed = blocks.filter((b) => b.failed);
  const blocked = blocks.filter((b) => b.blocked && !b.failed);
  const parts: string[] = [];
  if (typeof r.credit_added === 'number') parts.push(`新增 +${r.credit_added} 积分`);
  if (blocked.length) parts.push(`同人已领跳过 ${blocked.length} 个`);
  const desc = parts.join(' · ') || undefined;

  if (!failed.length) {
    // 「处理完成」而不是「成功」：已领过 / 暂无可领项的账号也会走到这里
    notify.ok(`活动全量：${total} 个账号处理完成`, desc || '无失败账号');
  } else if (failed.length >= total) {
    notify.err(`活动全量：${total} 个账号全部失败`, failed[0]?.error || '详见任务日志');
  } else {
    notify.warn(
      `活动全量：成功 ${total - failed.length}/${total}`,
      `失败：${failed.map((b) => shortName(b.name)).join('、')}${desc ? ` · ${desc}` : ''}`,
    );
  }
}

/** Pro 福利包（/tasks/travel）结果汇报：区分「新增 / 已领 / 失败」。 */
function reportTravel(t: TravelResp) {
  const results = t.results || [];
  if (!results.length) {
    if (t.msg) notify.err('未能领取 Pro 福利包', t.msg);
    else notify.warn('Pro 福利包：没有可处理的账号', '账号都已领取或活动未开放');
    return;
  }
  const failed = results.filter((r) => (r.msg || '').trimStart().startsWith('!'));
  const earned = results.filter((r) => (r.reward_credit || 0) > 0);
  const neutral = results.length - failed.length - earned.length;
  const sum = earned.reduce((n, r) => n + (r.reward_credit || 0), 0);
  const head = `Pro 福利包：新增 ${earned.length} 个账号${sum ? `（+${sum} 积分）` : ''}`;

  if (!failed.length) {
    notify.ok(head, neutral ? `已领 / 不可领 ${neutral} 个` : undefined);
  } else if (failed.length >= results.length) {
    notify.err(
      `Pro 福利包：${results.length} 个账号全部失败`,
      failed[0]?.msg || '详见任务日志',
    );
  } else {
    notify.warn(
      head,
      `失败 ${failed.length} 个：${failed
        .map((r) => r.nickname || uid8(r.uid || ''))
        .join('、')}${neutral ? ` · 已领 / 不可领 ${neutral} 个` : ''}`,
    );
  }
}

/* ------------------------------------------------------------------ 主体 */

export function GrowthTasksPanel({
  realm,
  onChanged,
}: {
  /** 当前视图区域：用于 /diag/vm 的区域选择与卡片角标。 */
  realm?: Realm;
  onChanged?: () => void;
}) {
  const [uid, setUid] = React.useState('all');
  const [tasks, setTasks] = React.useState<TaskRow[]>([]);
  const [summary, setSummary] = React.useState<TaskSummary | null>(null);
  const [accounts, setAccounts] = React.useState<AccountOption[]>([]);
  const [viewMsg, setViewMsg] = React.useState('');
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState<'' | 'run' | 'travel'>('');
  const [logs, setLogs] = React.useState<string[]>([]);

  const load = React.useCallback(
    async (quiet = false) => {
      try {
        const qs = uid !== 'all' ? `?uid=${encodeURIComponent(uid)}` : '';
        const r = (await http.get(`/tasks${qs}`)) as TasksView;
        const list = r.accounts || [];
        setTasks(r.tasks || []);
        setSummary(r.summary || null);
        setAccounts(list);
        setViewMsg(r.msg || '');
        // 选中的账号被删除/禁用后，后端会回退到第一个账号；这里同步回批量视图
        if (uid !== 'all' && !list.some((a) => a.uid === uid)) setUid('all');
      } catch (e) {
        if (!quiet) notify.err('签到与福利数据加载失败', errText(e));
      } finally {
        setLoading(false);
      }
    },
    [uid],
  );

  useAuthedLoad(() => {
    setLoading(true);
    void load();
  }, [load]);

  const appendLogs = (chunk: string[]) => {
    setLogs((prev) => [...prev, ...chunk].slice(-400));
  };

  const selected = accounts.find((a) => a.uid === uid);
  const label =
    uid === 'all' ? '全部账号' : selected?.nickname || uid8(uid);

  /** 「领取全部福利」：活动全量（/tasks/run）→ Pro 福利包（/tasks/travel）。 */
  const runAll = async () => {
    if (busy) return;
    setBusy('run');
    const chunks: string[] = [`====== 领取全部福利（${label}） ======`];
    let authFailed = false;
    try {
      chunks.push('------ 活动全量（/tasks/run） ------');
      try {
        const r = (await api.tasksRun(uid === 'all' ? undefined : uid)) as RunResp;
        (r.logs || []).forEach((l) => chunks.push(l));
        reportRun(r);
      } catch (e) {
        authFailed = e instanceof ApiError && (e.status === 401 || e.status === 403);
        chunks.push(`! 活动全量领取失败：${errText(e)}`);
        notify.err('活动全量领取失败', errText(e));
      }
      // 两个动作相互独立：run 失败时 travel 仍继续（401/403 除外，避免重复登录弹窗）
      if (!authFailed) {
        chunks.push('------ Pro 福利包（/tasks/travel） ------');
        try {
          const t = (await api.tasksTravel(uid === 'all' ? undefined : uid)) as TravelResp;
          (t.logs || []).forEach((l) => chunks.push(l));
          reportTravel(t);
        } catch (e) {
          chunks.push(`! Pro 福利包领取失败：${errText(e)}`);
          notify.err('Pro 福利包领取失败', errText(e));
        }
      }
    } finally {
      appendLogs(chunks);
      setBusy('');
      await load(true);
      onChanged?.();
    }
  };

  /** 「仅领 Pro 福利包」：只调 /tasks/travel。 */
  const runTravel = async () => {
    if (busy) return;
    setBusy('travel');
    const chunks: string[] = [`====== 仅领 Pro 福利包（${label}） ======`];
    try {
      const t = (await api.tasksTravel(uid === 'all' ? undefined : uid)) as TravelResp;
      (t.logs || []).forEach((l) => chunks.push(l));
      reportTravel(t);
    } catch (e) {
      chunks.push(`! Pro 福利包领取失败：${errText(e)}`);
      notify.err('Pro 福利包领取失败', errText(e));
    } finally {
      appendLogs(chunks);
      setBusy('');
      await load(true);
      onChanged?.();
    }
  };

  const isAll = uid === 'all' || summary?.mode === 'all';
  const travel = summary?.travel || {};
  const breakdown = summary?.energy_breakdown || [];

  let energyHint: string | undefined;
  if (isAll) {
    energyHint = breakdown.length
      ? `${breakdown
          .slice(0, 2)
          .map((x) => `${x.nickname || uid8(x.uid || '')} ${x.remain ?? 0}`)
          .join(' ｜ ')}${breakdown.length > 2 ? ` 等 ${breakdown.length} 个账号` : ''}`
      : '全部账号合计';
  } else {
    energyHint = summary?.plan ? `套餐 ${summary.plan}` : undefined;
  }

  let proValue: React.ReactNode = '待确认';
  let proHint = '点「仅领 Pro 福利包」检查资格';
  let proTone: StatTone = 'neutral';
  if (travel.state === 'arrived') {
    proValue = '可领取！';
    proHint = `领取奖励 +${travel.reward_credit || 1800} 积分`;
    proTone = 'success';
  } else if (travel.daily_limit_reached) {
    proValue = '已领取 / 活动结束';
    proHint = '一次性福利，不可重复领取';
  } else if (travel.state === 'traveling') {
    proValue = '处理中…';
    proHint = '稍后再来查看';
    proTone = 'info';
  }

  return (
    <div className="flex flex-col gap-4">
      {/* 头部：标题 + 账号选择 + 两个固定文案的领取按钮 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold tracking-[-0.01em]">签到与福利中心</h2>
            {realm && (
              <Badge variant="outline" className="rounded-full text-[10px]">
                {realm === 'intl' ? '国际版' : '国内版'}
              </Badge>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            每日活动直领 Credits 与兑换码；「领取全部福利」= 活动全量 + Pro
            福利包（一次性 +1800）。批量视图按活动聚合每账号资格。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={uid} onValueChange={setUid} disabled={busy !== ''}>
            <SelectTrigger size="sm" className="w-[190px]">
              <SelectValue placeholder="全部账号 (批量)" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部账号 (批量)</SelectItem>
              {accounts.map((a) => (
                <SelectItem key={a.uid} value={a.uid}>
                  {`${a.nickname || uid8(a.uid)} (${uid8(a.uid)} · ${
                    a.realm === 'intl' ? '国际版' : '国内版'
                  })`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            size="sm"
            className="h-8 rounded-full text-xs"
            disabled={busy !== ''}
            title="活动全量（/tasks/run）+ Pro 福利包（/tasks/travel）"
            onClick={() => void runAll()}
          >
            {busy === 'run' ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Gift className="size-3.5" />
            )}
            领取全部福利
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-8 rounded-full text-xs"
            disabled={busy !== ''}
            title="只领取一次性 Pro 福利包（/tasks/travel）"
            onClick={() => void runTravel()}
          >
            {busy === 'travel' ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Package className="size-3.5" />
            )}
            仅领 Pro 福利包
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-8 rounded-full"
            disabled={loading || busy !== ''}
            title="刷新任务与活动列表"
            onClick={() => void load()}
          >
            <RefreshCw className={cn('size-4', loading && 'animate-spin')} />
          </Button>
        </div>
      </div>

      {/* 四张状态卡：连续签到 / 积分余额 / Pro 包 / 本机虚拟化 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4 md:gap-4">
        <StatCard
          label={isAll ? '连续签到（批量视图）' : '连续签到天数'}
          value={`${summary?.streak_days ?? 0} 天`}
          hint={isAll ? '批量视图按活动聚合，不统计连续天数' : undefined}
          icon={CalendarDays}
        />
        <StatCard
          label={isAll ? '合计积分余额（全部账号）' : '积分额度余额'}
          value={fmt(summary?.energy)}
          hint={energyHint}
          icon={Wallet}
          tone="info"
          delay={0.04}
        />
        <StatCard
          label="Pro 福利包状态"
          value={proValue}
          hint={proHint}
          icon={Package}
          tone={proTone}
          delay={0.08}
        />
        <VmStatusCard realm={realm} />
      </div>

      {/* 任务日志（动作完成后自动出现，最新一行在底部） */}
      <TaskLogBox lines={logs} />

      {/* 任务 / 活动列表 */}
      {loading && tasks.length === 0 ? (
        <Skeleton className="h-48 w-full rounded-[20px]" />
      ) : tasks.length === 0 ? (
        <EmptyState
          icon={Gift}
          title={viewMsg || '暂无任务或未登录账号'}
          description="账号池可用后，这里会列出每日签到、限时活动与 Pro 升级包。"
          className="flex flex-col items-center justify-center p-8 text-center"
        />
      ) : (
        <section className="overflow-hidden rounded-[20px] bg-muted">
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-2 pt-3">
            <div className="text-[11px] text-muted-foreground">
              共 {tasks.length} 项任务 / 活动
              {isAll ? ' · 按活动聚合，徽章为各账号资格（悬停看账号名）' : ''}
            </div>
            {loading && <span className="text-[11px] text-muted-foreground">加载中…</span>}
          </div>
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="pl-4 text-[11px] font-normal text-muted-foreground">
                  任务名称 / 代码
                </TableHead>
                <TableHead className="text-[11px] font-normal text-muted-foreground">
                  任务说明
                </TableHead>
                <TableHead className="text-[11px] font-normal text-muted-foreground">
                  进度
                </TableHead>
                <TableHead className="text-[11px] font-normal text-muted-foreground">
                  奖励
                </TableHead>
                <TableHead className="pr-4 text-[11px] font-normal text-muted-foreground">
                  状态
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tasks.map((t, i) => {
                const st = statusBadge(t);
                const aggregate =
                  !!t.accounts_by_state && Object.keys(t.accounts_by_state).length > 0;
                const progress = aggregate
                  ? '多账号'
                  : t.status === 'claimed' || t.status === 'completed'
                    ? '已达成'
                    : `${t.current ?? 0} / ${t.target ?? 1}`;
                return (
                  <TableRow
                    key={t.task_code || `task-${i}`}
                    className="border-b border-border/40"
                  >
                    <TableCell className="pl-4 align-top">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {t.jump_url ? (
                          <a
                            href={t.jump_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-sm font-medium hover:underline"
                            title="打开官方活动页"
                          >
                            {t.name || t.task_code || '未命名任务'}
                            <ExternalLink className="size-3 text-muted-foreground" />
                          </a>
                        ) : (
                          <span className="text-sm font-medium">
                            {t.name || t.task_code || '未命名任务'}
                          </span>
                        )}
                      </div>
                      <div className="font-mono text-[11px] text-muted-foreground">
                        {t.task_code || '-'}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-[360px] whitespace-normal text-xs text-muted-foreground">
                      {t.description || '-'}
                    </TableCell>
                    <TableCell className="text-xs tabular-nums">{progress}</TableCell>
                    <TableCell className="text-xs">
                      {t.reward_text ? (
                        <b className="text-violet-600 dark:text-violet-400">{t.reward_text}</b>
                      ) : t.reward_credit ? (
                        <>
                          <b className="text-violet-600 dark:text-violet-400">
                            +{t.reward_credit}
                          </b>
                          <span className="text-muted-foreground"> 积分</span>
                        </>
                      ) : t.reward_energy ? (
                        <span className="text-amber-600 dark:text-amber-400">
                          +{t.reward_energy} 能量
                        </span>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </TableCell>
                    <TableCell className="pr-4 align-top">
                      {aggregate ? (
                        <StateBadges by={t.accounts_by_state as Record<string, string[]>} />
                      ) : (
                        <Badge
                          variant="outline"
                          className={cn('rounded-full text-[11px]', st.className)}
                        >
                          {st.text}
                        </Badge>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </section>
      )}

      {/* 已领取的兑换码 / 券（复制 + 活动页/二维码） */}
      <RewardCodesPanel
        codes={(summary?.codes || []).map((c) => ({
          ...c,
          // 单账号视图的后端条目不带账号信息：用当前选中账号补齐，避免空账号列
          nickname: c.nickname || (!isAll ? selected?.nickname || undefined : undefined),
          account: c.account || (!isAll ? uid : undefined),
          realm: c.realm || (!isAll ? selected?.realm || undefined : undefined),
        }))}
      />
    </div>
  );
}
