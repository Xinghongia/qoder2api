'use client';

import * as React from 'react';
import {RefreshCw, Users, Zap} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {AddAccountBar} from '@/components/common/accounts/AddAccountBar';
import {AccountTable, type AccountRow} from '@/components/common/gateway/AccountTable';
import {GrowthTasksPanel} from '@/components/common/gateway/GrowthTasksPanel';
import {VmStatusCard} from '@/components/common/gateway/VmStatusCard';
import {api} from '@/lib/api';
import {useAddAccount} from '@/lib/add-account-context';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {useRealm} from '@/lib/realm-context';

/**
 * `/accounts` —— 账号管理（从原「网关与运维」页拆出）。
 *
 * 账号表 + 添加账号工具栏 + 签到与福利中心 + 本机虚拟化检测；
 * 区域由右上角 RealmToggle 决定。所有操作原地生效，不切换视图。
 */
export default function AccountsPage() {
  const {view} = useRealm();
  const {refreshKey} = useAddAccount();
  const [accounts, setAccounts] = React.useState<AccountRow[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState('');

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      // 账号列表 + 上游「本轮已签到」状态并行取：后者要打活动平台（20s 缓存、
      // 并行拉取），单独请求，账号表首屏不必等它。
      const [acc, round] = await Promise.all([
        api.accounts.list('all'),
        api.accounts
          .checkinState()
          .catch(() => null) as Promise<{accounts?: RoundState[]} | null>,
      ]);
      const merged = new Map<string, RoundState>();
      for (const s of round?.accounts || []) {
        if (s?.uid) merged.set(s.uid, s);
      }
      setAccounts(
        ((acc?.accounts as AccountRow[]) || []).map((a) => {
          const r = merged.get(a.uid);
          return r
            ? {...a, roundClaimed: r.claimed === true, roundNote: r.round_note}
            : a;
        }),
      );
    } catch (e) {
      notify.err('账号列表加载失败', e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useAuthedLoad(load, [load, refreshKey]);

  const rows = React.useMemo(() => accounts.filter((a) => a.realm === view), [accounts, view]);
  const realmLabel = view === 'intl' ? '国际版' : '国内版';

  const onCheckin = async (uid?: string) => {
    setBusy(uid || 'all');
    try {
      const r = (await api.accounts.checkin(uid)) as {results?: CheckinResult[]};
      const results = r?.results || [];
      // 反馈口径照旧看板：逐个账号报「成功 / 失败原因」，不能只回一句
      // 「已提交」——失败（同人去重、上游拒绝、无接口）必须如实说出来。
      if (!results.length) {
        notify.warn(uid ? '未找到该账号' : '未发现可签到账号', '账号可能已停用或凭证缺失');
      } else if (uid || results.length === 1) {
        const x = results[0];
        const line = checkinLine(x);
        if (x.ok) notify.ok('签到完成', line);
        else notify.err('签到失败', line);
      } else {
        const lines = results.map(checkinLine).join('；');
        const okCount = results.filter((x) => x.ok).length;
        if (okCount === results.length) {
          notify.ok(`每日签到：${okCount}/${results.length} 全部成功`, lines);
        } else if (okCount > 0) {
          notify.warn(
            `每日签到：${okCount}/${results.length} 成功，${results.length - okCount} 个失败`,
            lines,
          );
        } else {
          notify.err('每日签到：全部失败', lines);
        }
      }
      await load();
    } catch (e) {
      notify.err('签到失败', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="账号"
        description={`${realmLabel}账号池 · 添加、签到与配额；所有操作原地生效`}
        actions={
          <>
            <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
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

      {loading && accounts.length === 0 ? (
        <Skeleton className="h-64 w-full rounded-[20px]" />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Users}
          title={`暂无${realmLabel}账号`}
          description="用「扫描本机凭证」或 OAuth / PAT 导入账号后即可开始转发。"
        />
      ) : (
        <AccountTable rows={rows} busy={busy} onCheckin={onCheckin} onChanged={load} />
      )}

      <GrowthTasksPanel realm={view} onChanged={load} />
      <VmStatusCard realm={view} />
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

/** 后端 /accounts/checkin 的单账号结果（qoder2api/api/accounts_routes.py）。 */
interface CheckinResult {
  uid?: string;
  nickname?: string;
  ok?: boolean;
  earned_credit?: number;
  /** logs 的最后一行（往往是「当前额度余额」这类收尾行） */
  msg?: string;
  logs?: string[];
}

/**
 * 一个账号的签到结果文案，与旧看板 checkinOne/doCheckin 同一口径：
 * 成功显示实际动作（领取/已领取），失败显示真实原因。
 * logs 里带 ✓/⚠/! 标记的那一行信息量最大，优先用它（去掉标记与 [账号名]）。
 */
function checkinLine(x: CheckinResult): string {
  const name = x.nickname || (x.uid || '').slice(0, 6) || '账号';
  const marked = (x.logs || []).find((l) => /[✓⚠!]/.test(l));
  let detail = (marked || x.msg || '').replace(/^[✓⚠!\-\s]*\[[^\]]*\]\s*/, '').trim();
  if (!detail) detail = x.ok ? '签到成功' : '签到失败';
  if (x.ok && x.earned_credit) detail = `+${x.earned_credit} Credits · ${detail}`;
  return `${name}：${detail}`;
}
