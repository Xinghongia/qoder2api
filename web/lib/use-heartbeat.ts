'use client';

import * as React from 'react';

/**
 * 心跳刷新：页面停留期间按固定间隔重新拉取数据（对齐 workbuddy 面板的
 * useHeartbeat 语义）——有了它就不必靠用户手点「刷新」。
 *
 * 两个细节：
 *   · 标签页不可见时跳过（浏览器本来也会把定时器节流，不如明确跳过）；
 *   · 切回可见时立即补一次，避免盯着切走之前的旧数据。
 *
 * 刷新函数走 ref 转发：调用方每次渲染产生的新闭包不会重建定时器。
 */
export function useHeartbeat(fn: () => void, ms: number) {
  const ref = React.useRef(fn);
  ref.current = fn;

  React.useEffect(() => {
    const tick = () => {
      if (!document.hidden) ref.current();
    };
    const timer = window.setInterval(tick, ms);
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [ms]);
}
