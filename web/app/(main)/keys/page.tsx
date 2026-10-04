'use client';

import * as React from 'react';
import {RefreshCw} from 'lucide-react';

import {ApiKeysSection} from '@/components/common/keys/ApiKeysSection';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {errText, type SettingsSnapshot} from '@/components/common/settings/shared';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {api} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';

/**
 * `/keys` —— API 密钥页（从设置页独立出来）。
 *
 * 数据仍来自 GET /settings（后端没有独立的密钥列表接口），因此沿用
 * SettingsSnapshot 里的 api_keys 数组。增删改全部由 ApiKeysSection 内的
 * 既有实现负责（整表替换 POST /settings/save {api_keys: [...]}），本页只
 * 负责取数、刷新与把结果传给组件。
 */
export default function KeysPage() {
  const [data, setData] = React.useState<SettingsSnapshot | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState('');

  const load = React.useCallback(async (announce = false) => {
    setLoading(true);
    try {
      const snapshot = (await api.settings.get()) as SettingsSnapshot;
      setData(snapshot);
      setError('');
      if (announce) notify.ok('密钥列表已刷新');
    } catch (e) {
      const message = errText(e);
      if (announce) notify.err('读取密钥失败', message);
      else setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  // 面板未登录时不发业务请求（必然 401）；登录成功后 needsLogin 变 false /
  // sessionEpoch 变化，这里会自动补拉一次。
  const authedLoad = React.useCallback(() => {
    void load();
  }, [load]);
  useAuthedLoad(authedLoad, [load]);

  // 组件保存成功后重新拉取整份快照（服务器会规范化掩码、创建时间等字段）。
  const onChanged = React.useCallback(() => load(), [load]);

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="API 密钥"
        description="未绑定出口 = 跟随网关出口（含双区失效切换）；绑定 = 固定走该区。"
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

      <div className="rounded-[20px] bg-muted p-4 text-xs leading-5 text-muted-foreground">
        <div className="font-semibold text-foreground">出口绑定说明</div>
        <ul className="mt-2 list-inside list-disc space-y-1">
          <li>
            <span className="text-foreground">未绑定（跟随网关出口）</span>
            ：密钥跟随网关当前的出口模式；双区模式下优先区失效时自动切换到另一区。
          </li>
          <li>
            <span className="text-foreground">绑定国际版出口</span>
            ：该密钥固定走国际版，不参与失效自动切换，适合指定客户端固定区域。
          </li>
          <li>
            <span className="text-foreground">绑定国内版出口</span>
            ：该密钥固定走国内版，不参与失效自动切换，适合指定客户端固定区域。
          </li>
        </ul>
        <p className="mt-2">
          客户端把 Key 填进 <code className="font-mono text-foreground">Authorization: Bearer</code>{' '}
          请求头即可调用网关。
        </p>
      </div>

      {!data ? (
        loading ? (
          <Skeleton className="h-64 w-full rounded-[20px]" />
        ) : (
          <div className="rounded-[20px] bg-muted p-8 text-center">
            <div className="text-sm font-semibold">读取密钥失败</div>
            <div className="mt-1 text-xs text-muted-foreground">{error || '未知错误'}</div>
            <Button className="mt-4" size="sm" onClick={() => void load(true)}>
              重试
            </Button>
          </div>
        )
      ) : (
        <ApiKeysSection data={data} onChanged={onChanged} />
      )}
    </div>
  );
}
