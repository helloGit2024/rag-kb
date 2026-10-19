"use client";

/**
 * 知识库列表页（V0.1 · T25，站点首页 `/`）。
 *
 * 验收：页面能列出知识库，能新建。
 *
 * 契约要点（docs/openapi.yaml L165—212）：
 * - GET /knowledge-bases：分页信封 {items, total, page, page_size}，
 *   page 从 1 起、page_size 上限 100、默认 20 —— 故本页必须做分页，
 *   否则超过 20 个知识库的部分会被静默截断（看起来像"全都在"）。
 * - POST /knowledge-bases → 201 KnowledgeBase；重名 409 DUPLICATE_KB_NAME。
 * - 列表不按权限过滤（OPEN-QUESTIONS 第三节 #18：V0.1 列全部），
 *   切换身份只影响后续的文档/问答可见性，本页数据不变——
 *   但仍监听身份变化重新拉取，避免将来改了过滤口径时这里漏掉。
 *
 * 改/删入口本版不做（分别见 OPEN-QUESTIONS 第二节 #1、#8）；
 * 后端 PATCH / DELETE 已交付并经 T21 e2e 验证，需要时用 curl。
 */

import { useCallback, useEffect, useState } from "react";
import { KbCreateForm } from "@/components/kb-create-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useIdentityChange } from "@/components/identity-switcher";
import { ApiError, listKnowledgeBases, type KbListOut } from "@/lib/api";

const PAGE_SIZE = 20;

/**
 * 把契约里的 UTC 时间串（`...Z`）格式化成北京时间显示。
 *
 * 为什么不用 `toLocaleString`：容器时区（UTC）与浏览器时区（本地）不同，
 * 服务端渲染与客户端渲染会输出不同字符串 → hydration mismatch（T26 踩过
 * 同款坑）。纯算术换算 + 手工补零，两端结果必然一致。
 */
function formatBeijing(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso; // 形状异常就原样显示，不猜
  const shifted = new Date(d.getTime() + 8 * 60 * 60 * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${shifted.getUTCFullYear()}-${p(shifted.getUTCMonth() + 1)}-` +
    `${p(shifted.getUTCDate())} ${p(shifted.getUTCHours())}:` +
    `${p(shifted.getUTCMinutes())}`
  );
}

export default function KnowledgeBasesPage() {
  const [data, setData] = useState<KbListOut | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);

  const load = useCallback(async (targetPage: number) => {
    try {
      const resp = await listKnowledgeBases(targetPage, PAGE_SIZE);
      setData(resp);
      setError("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "知识库列表加载失败");
    }
  }, []);

  useEffect(() => {
    void load(page);
  }, [load, page]);

  useIdentityChange(() => {
    void load(page);
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="grid gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">知识库</h1>
        <Button
          variant={showForm ? "outline" : "default"}
          onClick={() => setShowForm((v) => !v)}
        >
          {showForm ? "收起" : "新建知识库"}
        </Button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {showForm && (
        <KbCreateForm
          onCreated={() => {
            setShowForm(false);
            // 列表按 id 倒序，新建的必在第 1 页。
            // 已在第 1 页时 setPage(1) 不改变状态、不触发 effect，手动拉一次；
            // 否则靠 page 变化的 effect 拉，避免同一次创建发两次请求。
            if (page === 1) void load(1);
            else setPage(1);
          }}
        />
      )}

      <Card>
        <CardHeader>
          <CardTitle>全部知识库</CardTitle>
          <CardDescription>
            {data ? `共 ${data.total} 个` : "加载中…"}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          {data && data.items.length > 0 && (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-2">名称</th>
                  <th className="py-2">描述</th>
                  <th className="py-2">默认可见范围</th>
                  <th className="py-2">文档数</th>
                  <th className="py-2">创建时间</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((kb) => (
                  <tr key={kb.id} className="border-b">
                    <td className="py-2 pr-4 font-medium">{kb.name}</td>
                    <td className="py-2 pr-4 text-muted-foreground">
                      {kb.description ?? "—"}
                    </td>
                    <td className="py-2 pr-4">
                      <Badge variant="secondary">{kb.default_scope}</Badge>
                    </td>
                    <td className="py-2 pr-4">{kb.doc_count}</td>
                    <td className="py-2 text-muted-foreground">
                      {formatBeijing(kb.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {data && data.items.length === 0 && (
            <p className="text-sm text-muted-foreground">
              还没有知识库，点右上角「新建知识库」创建第一个。
            </p>
          )}

          {/* 超过一页时才显示翻页控件；少于一页时控件纯属噪音 */}
          {data && totalPages > 1 && (
            <div className="flex items-center justify-end gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                上一页
              </Button>
              <span className="text-sm text-muted-foreground">
                第 {page} / {totalPages} 页
              </span>
              <Button
                size="sm"
                variant="outline"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                下一页
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
