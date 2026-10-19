"use client";

/**
 * 问答页（V0.1 · T27，路由 /chat）。
 *
 * 验收：提问 → 看到答案 → 点引用卡片展开原文片段；库里没有相关内容时显示拒答。
 *
 * 契约要点（docs/openapi.yaml L55—120、L1125—1149、L1319—1332）：
 * - POST /api/v1/query **非流式**（V0.1 明确不做流式/多轮），请求 {question}。
 * - 拒答是 HTTP 200 + refused=true、answer 恰为固定句式（R5）——那是"正常
 *   结果的一种"，不是错误，故用中性卡片渲染，不套错误样式/红色。
 * - 引用卡片默认折叠、点击展开 snippet（原文片段）——即验收里的"点引用"。
 * - ★ 后端 V-1 未实现 kb_ids，本页**不做知识库选择器**（否则是假接口，见
 *   api.ts 的说明）。
 *
 * 页面上只保留"当前这一条"问答：新一轮提问覆盖上一轮，不做多轮记忆
 * （V0.1 ❌ 清单），也不做历史列表。
 */

import { useRef, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
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
import {
  ApiError,
  askQuestion,
  type Citation,
  type QueryResult,
} from "@/lib/api";

const MAX_QUESTION_LEN = 2000; // 与契约 question.maxLength 一致

/** 一条引用卡片：折叠态显示 [序号] + 文档标题，点开才显示原文片段。 */
function CitationCard({ c }: { c: Citation }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border">
      <Button
        type="button"
        variant="ghost"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex h-auto w-full items-center justify-between gap-3 rounded-md px-3 py-2 text-left font-normal"
      >
        <span className="text-sm font-medium">
          [{c.index}] {c.doc_title}
        </span>
        <span className="text-xs text-muted-foreground">
          {open ? "收起原文" : "展开原文"}
        </span>
      </Button>
      {open && (
        <div className="border-t px-3 py-2 text-sm whitespace-pre-wrap text-muted-foreground">
          {c.snippet}
        </div>
      )}
    </div>
  );
}

export default function ChatPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // 请求令牌：提问 +1，身份切换也 +1。回调只认"自己那一版"令牌，
  // 避免旧身份的响应回来后覆盖当前页面（身份已换、可见范围已变）。
  const reqIdRef = useRef(0);

  useIdentityChange(() => {
    reqIdRef.current += 1; // 作废在飞的请求
    setQuestion("");
    setResult(null);
    setError("");
    setLoading(false);
  });

  async function ask() {
    const q = question.trim();
    if (!q || loading) return;
    const myReq = ++reqIdRef.current;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const resp = await askQuestion(q);
      if (reqIdRef.current !== myReq) return; // 已切身份/已重新提问，丢弃
      setResult(resp);
    } catch (e) {
      if (reqIdRef.current !== myReq) return;
      // 503（模型不可用）/ 401（未提供身份）等信封 message 已是给人看的话
      setError(e instanceof ApiError ? e.message : "问答失败（网络异常）");
    } finally {
      if (reqIdRef.current === myReq) setLoading(false);
    }
  }

  const tooLong = question.length > MAX_QUESTION_LEN;

  return (
    <div className="grid gap-6">
      <h1 className="text-xl font-semibold">问答</h1>

      <Card>
        <CardHeader>
          <CardTitle>向知识库提问</CardTitle>
          <CardDescription>
            只在你可见的文档范围内检索；资料里没有的会明确拒答
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              // 输入框里回车是换行，Ctrl/Cmd+Enter 才提交
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void ask();
              }
            }}
            rows={3}
            maxLength={MAX_QUESTION_LEN}
            placeholder="例如：差旅住宿费的标准是多少？"
            aria-label="问题"
            className="w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
          />
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {tooLong
                ? `已超出 ${MAX_QUESTION_LEN} 字上限`
                : `${question.length} / ${MAX_QUESTION_LEN}　Ctrl/⌘ + Enter 提交`}
            </span>
            <Button onClick={ask} disabled={loading || !question.trim() || tooLong}>
              {loading ? "生成中…" : "提问"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {loading && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            正在检索并生成回答…
          </CardContent>
        </Card>
      )}

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {result && result.refused && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Badge variant="secondary">拒答</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2">
            <p className="text-sm">{result.answer}</p>
            <p className="text-xs text-muted-foreground">
              知识库里没有能支撑这个问题的内容。
            </p>
          </CardContent>
        </Card>
      )}

      {result && !result.refused && (
        <Card>
          <CardHeader>
            <CardTitle>回答</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4">
            <p className="text-sm leading-6 whitespace-pre-wrap">
              {result.answer}
            </p>
            {result.citations.length > 0 && (
              <div className="grid gap-2">
                <span className="text-sm font-medium text-muted-foreground">
                  引用（点击展开原文片段）
                </span>
                {result.citations.map((c) => (
                  <CitationCard key={c.index} c={c} />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
