'use client';

import * as React from 'react';
import {RefreshCw} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {ApiKeysSection} from '@/components/common/settings/ApiKeysSection';
import {IdentitySection} from '@/components/common/settings/IdentitySection';
import {PasswordSection} from '@/components/common/settings/PasswordSection';
import {ProxySection} from '@/components/common/settings/ProxySection';
import {RuntimeSection} from '@/components/common/settings/RuntimeSection';
import {errText, type SettingsSnapshot} from '@/components/common/settings/shared';
import {api} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {notify} from '@/lib/toast';

/**
 * `/settings` —— 网关设置页（旧看板 #pageSettings 的功能基准）。
 *
 * 五节：面板访问密码 / 网络代理 / 机器身份 / API Key 与出口绑定 / 运行信息。
 * 数据来自 GET /settings（qoder2api/views.py runtime_settings_view），
 * 各节的保存都走 POST /settings/save，成功后重新拉取整页快照。
 */
export default function SettingsPage() {
  const {ready, needsLogin} = useAuth();
  const [data, setData] = React.useState<SettingsSnapshot | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState('');

  const load = React.useCallback(async (announce = false) => {
    setLoading(true);
    try {
      const snapshot = (await api.settings.get()) as SettingsSnapshot;
      setData(snapshot);
      setError('');
      if (announce) notify.ok('设置已刷新');
    } catch (e) {
      const message = errText(e);
      if (announce) notify.err('读取设置失败', message);
      else setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  // 面板未登录时不发业务请求（必然 401）；登录成功后 needsLogin 变 false，
  // 这里会自动补拉一次，用户不必手动点「重试」。
  React.useEffect(() => {
    if (ready && !needsLogin) void load();
  }, [ready, needsLogin, load]);

  const onChanged = React.useCallback(() => load(), [load]);

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="网关设置"
        description="面板密码、网络代理、机器身份与 API Key；改动保存后立即生效。"
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void load(true)}
            disabled={loading}
          >
            <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
            刷新
          </Button>
        }
      />

      {!data ? (
        loading ? (
          <Skeleton className="h-64 w-full rounded-[20px]" />
        ) : (
          <div className="rounded-[20px] bg-muted p-8 text-center">
            <div className="text-sm font-semibold">读取设置失败</div>
            <div className="mt-1 text-xs text-muted-foreground">{error || '未知错误'}</div>
            <Button className="mt-4" size="sm" onClick={() => void load(true)}>
              重试
            </Button>
          </div>
        )
      ) : (
        <>
          <PasswordSection isDefault={data.panel_password_is_default} onChanged={onChanged} />
          <ProxySection data={data} onChanged={onChanged} />
          <IdentitySection data={data} onChanged={onChanged} />
          <ApiKeysSection data={data} onChanged={onChanged} />
          <RuntimeSection data={data} />
        </>
      )}
    </div>
  );
}
