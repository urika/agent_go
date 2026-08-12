/**
 * 企业门户新闻库 —— 筛选/搜索/排序纯函数，无 React 依赖。
 */

export interface NewsItem {
  id: string;
  title: string;
  category: string; // "公司动态" | "行业资讯" | "产品发布" | "公告"
  publishDate: string; // ISO 或 YYYY-MM-DD
  summary: string;
  tags: string[];
}

export interface NewsFilter {
  category?: string;
  keyword?: string;
  sortBy?: "date" | "title";
  order?: "asc" | "desc";
}

/** 按类别筛选 */
export function filterByCategory(items: NewsItem[], category?: string): NewsItem[] {
  if (!category || category === "全部") return items;
  return items.filter((n) => n.category === category);
}

/** 按关键词搜索标题 + 摘要 + 标签 */
export function searchItems(items: NewsItem[], keyword?: string): NewsItem[] {
  if (!keyword || !keyword.trim()) return items;
  const kw = keyword.trim().toLowerCase();
  return items.filter(
    (n) =>
      n.title.toLowerCase().includes(kw) ||
      n.summary.toLowerCase().includes(kw) ||
      n.tags.some((t) => t.toLowerCase().includes(kw)),
  );
}

/** 排序：date（按发布日期）或 title（按标题），order 控制升降序 */
export function sortItems(items: NewsItem[], sortBy: "date" | "title" = "date", order: "asc" | "desc" = "desc"): NewsItem[] {
  const sorted = [...items];
  sorted.sort((a, b) => {
    let cmp: number;
    if (sortBy === "title") {
      cmp = a.title.localeCompare(b.title, "zh-CN");
    } else {
      const at = new Date(a.publishDate).getTime();
      const bt = new Date(b.publishDate).getTime();
      cmp = at - bt;
    }
    return order === "asc" ? cmp : -cmp;
  });
  return sorted;
}

/** 组合筛选 + 搜索 + 排序 */
export function getFilteredNews(items: NewsItem[], filter: NewsFilter = {}): NewsItem[] {
  let result = filterByCategory(items, filter.category);
  result = searchItems(result, filter.keyword);
  result = sortItems(result, filter.sortBy ?? "date", filter.order ?? "desc");
  return result;
}

/** 分页（page 从 1 开始） */
export function paginate<T>(items: T[], page = 1, pageSize = 10): T[] {
  if (page < 1 || pageSize < 1) return [];
  const start = (page - 1) * pageSize;
  return items.slice(start, start + pageSize);
}

/** 提取标签云（按出现次数排序） */
export function tagCloud(items: NewsItem[], maxTags = 10): { tag: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const n of items) {
    for (const t of n.tags) {
      counts.set(t, (counts.get(t) ?? 0) + 1);
    }
  }
  return [...counts.entries()]
    .map(([tag, count]) => ({ tag, count }))
    .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag, "zh-CN"))
    .slice(0, maxTags);
}
