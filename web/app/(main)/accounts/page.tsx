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
      const acc = await api.accounts.list('all');
      setAccounts((acc?.accounts as AccountRow[]) || []);
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
      const r = await api.accounts.checkin(uid);
      notify.ok(uid ? '已提交签到' : '已提交全部签到', describeCheckin(r));
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

function describeCheckin(r: unknown): string | undefined {
  const data = r as {results?: Array<{uid?: string; ok?: boolean; reward_text?: string}>};
  if (!data?.results?.length) return undefined;
  const okCount = data.results.filter((x) => x.ok).length;
  return `${okCount}/${data.results.length} 个账号成功`;
}
