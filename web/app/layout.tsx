import type {Metadata, Viewport} from 'next';

import {AuthProvider} from '@/lib/auth-context';
import {RealmProvider} from '@/lib/realm-context';
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
              {children}
              <Toaster />
            </AuthProvider>
          </RealmProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
