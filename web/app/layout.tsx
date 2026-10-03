import type {Metadata, Viewport} from 'next';

import {AuthProvider, PanelLoginDialog} from '@/lib/auth-context';
import {RealmProvider} from '@/lib/realm-context';
import {AppDock} from '@/components/common/layout/AppDock';
import {ThemeProvider} from '@/components/common/layout/ThemeProvider';
import {Toaster} from '@/components/ui/sonner';

import './globals.css';

export const metadata: Metadata = {
  title: 'Qoder 网关看板',
  description: 'Qoder 多账号反向代理网关（国内版 / 国际版）',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({children}: {children: React.ReactNode}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="font-sans antialiased">
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
          <RealmProvider>
            <AuthProvider>
              <div className="@container/main flex min-h-screen flex-col">
                <div className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-4 px-4 pb-28 pt-8 sm:px-6 md:px-8 md:gap-6 lg:px-12">
                  {children}
                </div>
              </div>
              <PanelLoginDialog />
              <AppDock />
              <Toaster />
            </AuthProvider>
          </RealmProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
