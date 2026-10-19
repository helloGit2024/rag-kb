import Link from "next/link";

// V0.1 三页入口：知识库列表（T25 待建）/ 文档上传（T26）/ 问答（T27 待建）。
// T25 完成后首页会替换为知识库列表。
export default function Home() {
  return (
    <div className="grid gap-4">
      <h1 className="text-xl font-semibold">企业知识库</h1>
      <nav className="grid gap-2">
        <Link href="/documents" className="text-blue-600 hover:underline">
          文档上传
        </Link>
        <span className="text-muted-foreground">知识库列表（T25 待建）</span>
        <span className="text-muted-foreground">问答（T27 待建）</span>
      </nav>
    </div>
  );
}
