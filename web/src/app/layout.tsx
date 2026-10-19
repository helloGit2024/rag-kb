import type { Metadata } from "next";
import Link from "next/link";
import { IdentitySwitcher } from "@/components/identity-switcher";
import "./globals.css";

export const metadata: Metadata = {
  title: "企业知识库",
  description: "企业知识库问答平台（V0.1）",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // 内网部署不依赖 Google Fonts（build 时拉不到会直接失败），
  // 用系统字体栈替代 next/font/google 的 Geist。
  //
  // suppressHydrationWarning：浏览器扩展（如"通义"）会在 React hydration
  // 之前给文档骨架注入属性——已确认它会改 <html>（className/data-theme/
  // style），而同类扩展也常给 <body> 加 class。属性不一致会让 React 报
  // hydration mismatch。
  // ★ html 与 body **各加一次**：该属性只让 React 跳过"本元素自身"的属性
  //   比对，不递归到子元素——所以它不会掩盖子组件的真实错配，只挡掉扩展
  //   在骨架上的注入。这是 Next.js 官方对这一场景的推荐处理。
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="antialiased" suppressHydrationWarning>
        <header className="border-b">
          <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
            <nav className="flex items-center gap-6">
              <Link href="/" className="font-semibold">
                企业知识库
              </Link>
              {/* V0.1 三页：知识库列表（首页 /，T25）/ 文档上传 / 问答（T27 待建） */}
              <Link
                href="/"
                className="text-sm text-muted-foreground hover:text-foreground"
              >
                知识库
              </Link>
              <Link
                href="/documents"
                className="text-sm text-muted-foreground hover:text-foreground"
              >
                文档上传
              </Link>
            </nav>
            <IdentitySwitcher />
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
      </body>
    </html>
  );
}
