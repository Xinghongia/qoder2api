'use client';

import * as React from 'react';
import {ArrowLeftRight, Globe, Loader2} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {useRealm, type Realm, type RealmMode} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

/**
 * 网关出口控制卡（仪表盘顶部）。
 *
 * 四选一，与旧看板 `realmModeSelect` 完全一致：
 *   仅国际版 / 仅国内版 / 双区 · 优先国际版 / 双区 · 优先国内版
 * 写入 POST /realm {mode, preferred}（持久化到 accounts/active_realm.json）。
 * 双区模式下首选区失效会自动切到另一区，恢复后切回。
 */

const OPTIONS = [
  {value: 'intl', mode: 'intl', preferred: 'intl', label: '仅国际版'},
  {value: 'cn', mode: 'cn', preferred: 'cn', label: '仅国内版'},
  {value: 'both:intl', mode: 'both', preferred: 'intl', label: '双区 · 优先国际版'},
  {value: 'both:cn', mode: 'both', preferred: 'cn', label: '双区 · 优先国内版'},
] as const;

export function GatewayExitCard() {
  const {mode, preferred, setMode} = useRealm();
  const [busy, setBusy] = React.useState(false);

  const value = mode === 'both' ? `both:${preferred}` : mode;
  const current = OPTIONS.find((o) => o.value === value) || OPTIONS[1];
  const exportLabel =
    mode === 'both'
      ? `双区（优先${preferred === 'intl' ? '国际版' : '国内版'}，失效自动切换）`
      : mode === 'intl'
        ? '仅国际版'
        : '仅国内版';

  const onChange = async (next: string) => {
    const opt = OPTIONS.find((o) => o.value === next);
    if (!opt || busy) return;
    setBusy(true);
    try {
      await setMode(opt.mode as RealmMode, opt.preferred as Realm);
      notify.ok('网关出口已保存并生效', opt.label);
    } catch (e) {
      notify.err('切换失败', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="flex flex-wrap items-center gap-4 rounded-[20px] bg-muted px-4 py-3.5">
      <div className="grid size-10 shrink-0 place-items-center rounded-full bg-white/70 dark:bg-white/[0.05]">
        <Globe className="size-5 text-blue-600 dark:text-blue-400" />
      </div>

      <div className="min-w-0">
        <div className="text-[11px] font-medium text-muted-foreground">网关默认出口</div>
        <div className="mt-0.5 text-lg font-semibold tracking-[-0.02em]">{current.label.split(' · ')[0]}</div>
        <div className="mt-0.5 text-[11px] text-muted-foreground">{exportLabel}</div>
      </div>

      <div className="ml-auto flex items-center gap-2">
        {busy && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
        <Badge variant="outline" className={cn('hidden rounded-full text-[10px] sm:inline-flex',
          mode === 'both' ? 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-400' : '')}>
          <ArrowLeftRight className="mr-1 size-3" />
          {mode === 'both' ? '双区' : '单区'}
        </Badge>
        <Select value={value} onValueChange={(v) => void onChange(v)} disabled={busy}>
          <SelectTrigger className="h-9 w-[190px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {OPTIONS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </section>
  );
}
