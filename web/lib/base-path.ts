/**
 * 部署基础路径。默认部署在根路径（空串），行为与不引入本模块时完全一致；
 * 需要挂到子路径时在构建时设置 `NEXT_PUBLIC_BASE_PATH`。
 */
export const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH || '').replace(/\/+$/, '');

/** 给站内绝对路径补上 basePath（外部链接、锚点、相对路径原样返回）。 */
export function withBasePath(path: string): string {
  if (!BASE_PATH || !path.startsWith('/') || path.startsWith('//')) return path;
  if (path === BASE_PATH || path.startsWith(`${BASE_PATH}/`)) return path;
  return `${BASE_PATH}${path}`;
}
