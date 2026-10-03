'use client';

import * as React from 'react';
import {TriangleAlert} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {api} from '@/lib/api';
import {notify} from '@/lib/toast';
import {FIELD_CLASS, FieldLabel, MonoValue, SectionCard, errText, type SettingsSnapshot} from './shared';

type ProxyMode = 'system' | 'manual' | 'direct';

const MODES: Array<{value: ProxyMode; label: string}> = [
  {value: 'system', label: '跟随系统代理'},
  {value: 'manual', label: '手动指定代理'},
  {value: 'direct', label: '直连（不使用代理）'},
];

function normalizeMode(mode: string): ProxyMode {
  return mode === 'manual' || mode === 'direct' ? mode : 'system';
}

/**
 * 网络代理（三档）。后端：POST /settings/save {proxy_mode, proxy_url}，
 * 立即调用 qoder_net.configure() 生效；环境变量 QD_PROXY_MODE / QD_PROXY_URL
 * 存在时优先级更高（此时页面保存的模式不会真正改变出站路径）。
 */
export function ProxySection({
  data,
  onChanged,
}: {
  data: SettingsSnapshot;
  onChanged: () => Promise<void>;
}) {
  const proxy = data.proxy;
  const [mode, setMode] = React.useState<ProxyMode>(normalizeMode(proxy.mode));
  const [url, setUrl] = React.useState(proxy.url || '');
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    setMode(normalizeMode(proxy.mode));
    setUrl(proxy.url || '');
  }, [proxy.mode, proxy.url]);

  const save = async () => {
    const trimmed = url.trim();
    if (mode === 'manual' && !trimmed) {
      notify.warn('手动代理模式需要填写代理地址', '如 http://127.0.0.1:7897');
      return;
    }
    setBusy(true);
    try {
      const r = (await api.settings.save({
        proxy_mode: mode,
        proxy_url: trimmed,
      })) as {proxy_saved?: string} | undefined;
      notify.ok('代理设置已保存并即时生效', r?.proxy_saved);
      await onChanged();
    } catch (e) {
      notify.err('保存代理设置失败', errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SectionCard
      title="网络代理"
      hint={
        '网关所有出站请求（模型推理、活动领取、账号接口、模型清单）统一走这里的选择；本机回环地址始终直连。环境变量 QD_PROXY_MODE / QD_PROXY_URL 存在时优先级更高。'
      }
      aside={
        proxy.env_override ? (
          <Badge variant="secondary" className="border-transparent bg-amber-500/12 text-amber-700 dark:text-amber-400">
            被环境变量覆盖
          </Badge>
        ) : undefined
      }
      delay={0.04}
    >
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        {MODES.map((item) => (
          <label key={item.value} className="flex cursor-pointer items-center gap-2 text-xs">
            <input
              type="radio"
              name="settings-proxy-mode"
              className="size-3.5 accent-[var(--primary)]"
              checked={mode === item.value}
              onChange={() => setMode(item.value)}
            />
            {item.label}
          </label>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-end gap-3">
        <div className="min-w-[240px] flex-1">
          <FieldLabel>代理地址（仅手动模式使用）</FieldLabel>
          <Input
            className={FIELD_CLASS}
            placeholder="http://127.0.0.1:7897"
            value={url}
            disabled={mode !== 'manual'}
            onChange={(e) => setUrl(e.target.value)}
          />
        </div>
        <Button size="sm" onClick={() => void save()} disabled={busy}>
          {busy ? '保存中…' : '保存代理设置'}
        </Button>
      </div>

      <div className="mt-3 flex items-start gap-2 text-xs leading-5 text-muted-foreground">
        {proxy.env_override && <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />}
        <span>
          当前生效：<MonoValue className="text-muted-foreground">{proxy.effective || '-'}</MonoValue>
          {proxy.env_override && '（面板保存的模式暂不生效，请先取消环境变量）'}
        </span>
      </div>
    </SectionCard>
  );
}
