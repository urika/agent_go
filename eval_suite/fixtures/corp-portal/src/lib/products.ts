export interface Product {
  id: string;
  name: string;
  category: string; // "硬件" | "软件" | "服务"
  price: number; // 万元
  description: string;
  inStock: boolean;
  rating: number; // 0-5
}

export const PRODUCTS: Product[] = [
  { id: "p1", name: "智能工业网关 GW-100", category: "硬件", price: 1.2, description: "边缘智能网关，支持多协议接入", inStock: true, rating: 4.8 },
  { id: "p2", name: "边缘计算一体机 EC-200", category: "硬件", price: 3.5, description: "一体化工控机，内置 AI 推理能力", inStock: true, rating: 4.6 },
  { id: "p3", name: "工业物联网平台 IoTSuite", category: "软件", price: 8.0, description: "设备管理/数据采集/可视化一体化平台", inStock: true, rating: 4.7 },
  { id: "p4", name: "智能制造 MES 系统", category: "软件", price: 15.0, description: "生产执行管理系统，支持排程/追溯/看板", inStock: false, rating: 4.5 },
  { id: "p5", name: "数字化转型咨询", category: "服务", price: 20.0, description: "企业数字化战略规划与实施咨询", inStock: true, rating: 4.9 },
  { id: "p6", name: "远程运维服务", category: "服务", price: 2.0, description: "7x24 远程监控与故障响应", inStock: true, rating: 4.4 },
  { id: "p7", name: "智能传感器套件 SE-300", category: "硬件", price: 0.5, description: "温湿度/振动/电流多合一传感器", inStock: false, rating: 4.2 },
  { id: "p8", name: "数据中台解决方案", category: "软件", price: 25.0, description: "数据集成/治理/服务一体化平台", inStock: true, rating: 4.3 },
];

/** 产品筛选：类别 + 仅看有货 */
export function filterProducts(
  products: Product[],
  opts: { category?: string; inStockOnly?: boolean } = {},
): Product[] {
  let result = products;
  if (opts.category && opts.category !== "全部") {
    result = result.filter((p) => p.category === opts.category);
  }
  if (opts.inStockOnly) {
    result = result.filter((p) => p.inStock);
  }
  return result;
}

/** 产品排序：价格或评分 */
export function sortProducts(
  products: Product[],
  sortBy: "price" | "rating" = "price",
  desc = true,
): Product[] {
  return [...products].sort((a, b) => {
    const cmp = a[sortBy] - b[sortBy];
    return desc ? -cmp : cmp;
  });
}

/** 统计信息：总数/有货数/类别分布 */
export function productStats(products: Product[]): {
  total: number;
  inStock: number;
  categories: Record<string, number>;
} {
  const categories: Record<string, number> = {};
  for (const p of products) {
    categories[p.category] = (categories[p.category] ?? 0) + 1;
  }
  return {
    total: products.length,
    inStock: products.filter((p) => p.inStock).length,
    categories,
  };
}

/** 平均评分 */
export function avgRating(products: Product[]): number {
  if (!products.length) return 0;
  return products.reduce((s, p) => s + p.rating, 0) / products.length;
}
