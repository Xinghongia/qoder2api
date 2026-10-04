'use client';

import Link from 'next/link';
import {usePathname} from 'next/navigation';
import {
  BarChart3,
  Boxes,
  KeyRound,
  LayoutDashboard,
  Moon,
  PlusCircle,
  ScrollText,
  Settings as SettingsIcon,
  Sun,
  Users,
} from 'lucide-react';
import {useTheme} from 'next-themes';

import {FloatingDock, type FloatingDockItem} from '@/components/ui/floating-dock';
import {useAddAccount} from '@/lib/add-account-context';
import {cn} from '@/lib/utils';

/**
 * 底部管理栏：分组与 workbuddy-manager 一致 —— 总览 / 运营 / 治理 / 动作，
 * 组间由 FloatingDock 的 GroupDivider 画分隔线（hover 显示组名）。
 */
const NAV = [
  {href: '/', title: '仪表盘', icon: LayoutDashboard, group: '总览'},
  {href: '/accounts', title: '账号', icon: Users, group: '运营'},
  {href: '/keys', title: '密钥', icon: KeyRound, group: '运营'},
  {href: '/models', title: '模型', icon: Boxes, group: '运营'},
  {href: '/stats', title: '统计', icon: BarChart3, group: '治理'},
  {href: '/logs', title: '日志', icon: ScrollText, group: '治理'},
  {href: '/settings', title: '设置', icon: SettingsIcon, group: '治理'},
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
  const {open} = useAddAccount();
  const active = (href: string) => (href === '/' ? pathname === '/' : pathname.startsWith(href));

  const items: FloatingDockItem[] = NAV.map((item) => ({
    title: item.title,
    icon: <item.icon className="size-5" />,
    href: item.href,
    tooltip: item.title,
    groupKey: item.group,
    groupLabel: item.group,
    customComponent: (
      <Link
        href={item.href}
        aria-current={active(item.href) ? 'page' : undefined}
        className={cn(
          'grid size-full place-items-center rounded-full transition-colors',
          // 选中态：亮底 + 常规前景色（原先的 bg-primary 是一整块深色圆，
          // 在浅色主题下像一块黑补丁；改成浅色胶囊后图标更"透气"）。
          active(item.href)
            ? 'bg-white text-foreground shadow-sm ring-1 ring-black/5 dark:bg-white/[0.14] dark:text-white dark:ring-white/10'
            : 'text-neutral-600 hover:text-neutral-900 dark:text-neutral-300 dark:hover:text-white',
        )}
      >
        <item.icon className="size-4" />
      </Link>
    ),
  }));

  // 动作组：+ 添加账号 / 主题切换（与 workbuddy 的 actions 组同构）
  items.push({
    title: '添加账号',
    icon: <PlusCircle className="size-5" />,
    tooltip: '添加账号（OAuth / 扫描 / PAT / JSON）',
    groupKey: '动作',
    customComponent: (
      <button
        type="button"
        onClick={() => open('oauth')}
        className="grid size-full place-items-center rounded-full text-neutral-600 transition-colors hover:text-neutral-900 dark:text-neutral-300 dark:hover:text-white"
      >
        <PlusCircle className="size-4" />
      </button>
    ),
  });
  items.push({
    title: '主题',
    icon: <ThemeToggle />,
    groupKey: '动作',
    customComponent: <ThemeToggle />,
  });

  return (
    // 外层 fixed 必须「收缩到内容宽度」再居中（left-1/2 + -translate-x-1/2）：
    // 若写成 inset-x-0，浮岛会被拉成整屏宽的一条，图标全挤在左边。
    <div className="fixed bottom-3 left-1/2 z-40 w-fit -translate-x-1/2">
      <FloatingDock
        items={items}
        desktopClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-16 gap-2 rounded-2xl px-4"
        mobileButtonClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-12 w-12"
      />
    </div>
  );
}
