import type {NextConfig} from 'next';

/**
 * 静态导出（生产）与开发代理（开发）二选一。
 *
 * - 生产：`NEXT_OUTPUT_EXPORT=1 next build` → `web/out/`，由 Python 后端
 *   （qoder2api/api/static.py）直接托管，最终用户不需要 Node。
 * - 开发：`next dev` 把网关的 API 前缀反向代理到本机 8790，前后端同源，
 *   不需要后端开 CORS。
 */
const isExport = process.env.NEXT_OUTPUT_EXPORT === '1';
const backend = process.env.NEXT_PUBLIC_BACKEND_BASE_URL || 'http://127.0.0.1:8790';

const API_PREFIXES = [
  'v1',
  'accounts',
  'usage',
  'tasks',
  'scheduler',
  'settings',
  'logs',
  'panel',
  'realm',
  'diag',
  'identity',
  'update',
];

const nextConfig: NextConfig = {
  ...(isExport
    ? {output: 'export' as const, trailingSlash: true}
    : {
        async rewrites() {
          return [
            ...API_PREFIXES.map((p) => ({
              source: `/${p}/:path*`,
              destination: `${backend}/${p}/:path*`,
            })),
            {source: '/realm', destination: `${backend}/realm`},
            {source: '/health', destination: `${backend}/health`},
            {source: '/ping', destination: `${backend}/ping`},
          ];
        },
      }),
  images: {unoptimized: true, remotePatterns: []},
  env: {
    NEXT_PUBLIC_BUILD_TIME: new Date().toISOString(),
  },
};

export default nextConfig;
