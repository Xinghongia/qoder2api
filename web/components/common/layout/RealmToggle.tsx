'use client';

import {motion} from 'motion/react';

import {useRealm, type Realm} from '@/lib/realm-context';
import {cn} from '@/lib/utils';

const REALMS: {key: Realm; label: string}[] = [
  {key: 'intl', label: '国际版'},
  {key: 'cn', label: '国内版'},
];

/**
 * 查看区域切换（国际版 / 国内版）：右上角常驻，作用于仪表盘 / 账号 / 模型 / 统计。
 * 只改「在看哪个区」，不修改网关出口模式（那是仪表盘上的四选一）。
 */
export function RealmToggle() {
  const {view, setView} = useRealm();

  return (
    <div
      role="tablist"
      aria-label="查看区域"
      className="inline-flex items-center gap-0.5 rounded-full bg-muted p-1 text-muted-foreground"
    >
      {REALMS.map((r) => {
        const active = view === r.key;
        return (
          <button
            key={r.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => setView(r.key)}
            className={cn(
              'relative rounded-full px-3 py-1 text-xs font-medium transition-colors',
              active ? 'text-foreground' : 'hover:text-foreground',
            )}
          >
            {active && (
              <motion.span
                layoutId="realm-pill"
                className="absolute inset-0 rounded-full bg-background shadow-sm dark:bg-white/[0.08]"
                transition={{type: 'spring', stiffness: 400, damping: 32}}
              />
            )}
            <span className="relative">{r.label}</span>
          </button>
        );
      })}
    </div>
  );
}
