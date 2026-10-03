'use client';

import Link from 'next/link';
import {usePathname} from 'next/navigation';
import {
  BarChart3,
  Moon,
  ScrollText,
  Server,
  Settings as SettingsIcon,
  Sun,
} from 'lucide-react';
import {useTheme} from 'next-themes';

import {FloatingDock, type FloatingDockItem} from '@/components/ui/floating-dock';
import {cn} from '@/lib/utils';

const NAV = [
  {href: '/', title: '网关与运维', icon: Server, group: '总览'},
  {href: '/stats', title: '数据指标', icon: BarChart3, group: '总览'},
  {href: '/logs', title: '日志', icon: ScrollText, group: '运维'},
  {href: '/settings', title: '设置', icon: SettingsIcon, group: '运维'},
];

function ThemeToggle() {
  const {resolvedTheme, setTheme} = useTheme();
  const dark = resolvedTheme === 'dark';
  return (
    <button
      type="button"
      aria-label={dark ? '切换到亮色' : '切换到暗色'}
      onClick={() => setTheme(dark ? 'light' : 'dark')}
      className={cn(
        'grid size-9 place-items-center rounded-full bg-gray-200 text-neutral-700 transition-colors',
        'hover:bg-gray-300 dark:bg-neutral-800 dark:text-neutral-200 dark:hover:bg-neutral-700',
      )}
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}

export function AppDock() {
  const pathname = usePathname() || '/';
  const active = (href: string) => (href === '/' ? pathname === '/' : pathname.startsWith(href));

  const items: FloatingDockItem[] = NAV.map((item) => ({
    title: item.title,
    icon: <item.icon className="size-5" />,
    href: item.href,
    tooltip: item.title,
    groupKey: item.group,
    groupLabel: item.group,
    // 用 Link 语义做客户端跳转；customComponent 里包一层 <Link> 保留 dock 动效
    customComponent: (
      <Link
        href={item.href}
        aria-current={active(item.href) ? 'page' : undefined}
        className={cn(
          'grid size-full place-items-center rounded-full transition-colors',
          active(item.href)
            ? 'bg-primary text-primary-foreground'
            : 'text-neutral-600 hover:text-neutral-900 dark:text-neutral-300 dark:hover:text-white',
        )}
      >
        <item.icon className="size-4" />
      </Link>
    ),
  }));

  items.push({
    title: '主题',
    icon: <ThemeToggle />,
    groupKey: '偏好',
    groupLabel: '偏好',
    customComponent: <ThemeToggle />,
  });

  return (
    // 外层 fixed 必须「收缩到内容宽度」再居中（left-1/2 + -translate-x-1/2）：
    // 若写成 inset-x-0，浮岛会被拉成整屏宽的一条，图标全挤在左边
    // （FloatingDock 桌面容器只有 mx-auto，没有自带宽度的约束）。
    <div className="fixed bottom-3 left-1/2 z-40 w-fit -translate-x-1/2">
      <FloatingDock
        items={items}
        desktopClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-16 gap-2 rounded-2xl px-4"
        mobileButtonClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-12 w-12"
      />
    </div>
  );
}
