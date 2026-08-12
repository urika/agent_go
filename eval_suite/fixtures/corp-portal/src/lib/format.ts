/**
 * 企业门户格式化工具库 —— 纯函数，无 React 依赖，可独立测试。
 */

/** 有限数校验：NaN / ±Infinity / 非 number 一律返回 null（与行情端 finite() 语义一致） */
export function finite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * 格式化数字为千分位字符串。
 * 例：1234567.891 → "1,234,567.89"（默认 2 位小数）
 * 无效值（null/NaN/Infinity）→ "—"
 */
export function formatNumber(v: number | null | undefined, decimals = 2): string {
  const n = finite(v);
  if (n === null) return "—";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/**
 * 格式化百分比（保留 decimals 位小数 + "%"）。
 * 例：0.1567 → "15.67%"
 * 无效值 → "—"
 */
export function formatPercent(v: number | null | undefined, decimals = 2): string {
  const n = finite(v);
  if (n === null) return "—";
  return `${(n * 100).toFixed(decimals)}%`;
}

/**
 * 格式化日期为 YYYY-MM-DD。
 * 输入：Date 或 ISO 字符串或 "YYYY-MM-DD"。
 * 无效日期 → "—"
 */
export function formatDate(v: Date | string | null | undefined): string {
  if (v == null) return "—";
  try {
    const d = typeof v === "string" ? new Date(v) : v;
    if (Number.isNaN(d.getTime())) return "—";
    const m = `${d.getMonth() + 1}`.padStart(2, "0");
    const day = `${d.getDate()}`.padStart(2, "0");
    return `${d.getFullYear()}-${m}-${day}`;
  } catch {
    return "—";
  }
}

/**
 * 相对时间：距现在多久。
 * 输入 ISO 字符串；返回 "刚刚"/"N 分钟前"/"N 小时前"/"N 天前"/日期。
 * 无效输入 → "—"
 */
export function timeAgo(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diffMs = now.getTime() - t;
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return formatDate(iso);
}

/**
 * 文本截断（保留首尾，中间省略号）。截断后总长 ≤ maxLen。
 * 例：truncateMiddle("abcdefghijkl", 8) → "abc…jkl"
 */
export function truncateMiddle(text: string, maxLen = 20): string {
  if (maxLen < 2) return text.slice(0, maxLen);
  if (text.length <= maxLen) return text;
  const head = Math.ceil((maxLen - 1) / 2);
  const tail = maxLen - 1 - head;
  return text.slice(0, head) + "…" + text.slice(-tail);
}
