import type { NextConfig } from "next";

// API 代理：浏览器只请求同源 /api/v1/*，由 Next 转发到 backend。
// 为什么走代理而不是直连：① 免 CORS 配置；② 后端地址只出现在服务端
// 环境变量里，不暴露给浏览器。目标默认 compose 服务名 backend:8000
//（容器网络内）；宿主 npm run dev 时用 BACKEND_ORIGIN=http://localhost:8000 覆盖。
const backendOrigin = process.env.BACKEND_ORIGIN ?? "http://backend:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${backendOrigin}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
