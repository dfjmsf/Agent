# Next.js 前端编码规范

## 项目结构（App Router — Next.js 13+）
```
src/
  app/
    layout.js          # 根布局（全局 HTML 结构）
    page.js            # 首页
    globals.css        # 全局样式
    api/
      users/
        route.js       # API Route: /api/users
    dashboard/
      page.js          # /dashboard 页面
      loading.js       # 加载状态
  components/
    Header.jsx         # 可复用组件
    UserCard.jsx
  lib/
    db.js              # 数据库连接
    utils.js           # 工具函数
  public/
    favicon.ico
  next.config.js
  package.json
```

## 核心规则

### 1. App Router vs Pages Router
- **默认使用 App Router**（`src/app/` 目录结构）
- 文件即路由：`app/about/page.js` → `/about`
- API 路由：`app/api/users/route.js` → `/api/users`
- 布局：`layout.js` 自动包裹同级及子级 `page.js`

### 2. Server Component vs Client Component
```jsx
// 默认是 Server Component（可以 async/await、直接查数据库）
export default async function UsersPage() {
  const users = await fetch('/api/users').then(r => r.json());
  return <ul>{users.map(u => <li key={u.id}>{u.name}</li>)}</ul>;
}

// 需要交互（useState/useEffect/onClick）时必须加 'use client'
'use client';
import { useState } from 'react';
export default function Counter() {
  const [count, setCount] = useState(0);
  return <button onClick={() => setCount(c => c + 1)}>{count}</button>;
}
```
- **铁律**：只有需要 `useState`/`useEffect`/事件处理器的组件才加 `'use client'`
- Server Component 禁止使用 React hooks
- Client Component 不能直接 import Server Component

### 3. API Routes（Route Handlers）
```javascript
// app/api/users/route.js
import { NextResponse } from 'next/server';

export async function GET(request) {
  const users = []; // 从数据库查询
  return NextResponse.json(users);
}

export async function POST(request) {
  const data = await request.json();
  // 写入数据库
  return NextResponse.json({ id: 1, ...data }, { status: 201 });
}
```
- 必须导出 HTTP 方法名的函数（`GET`, `POST`, `PUT`, `DELETE`）
- 使用 `NextResponse.json()` 返回数据
- 动态路由：`app/api/users/[id]/route.js`

### 4. 数据获取
```jsx
// Server Component 直接 fetch（自动去重 + 缓存）
const data = await fetch('https://api.example.com/data', {
  cache: 'no-store',  // 禁用缓存（实时数据）
  // next: { revalidate: 60 },  // ISR: 60秒重新验证
});

// Client Component 用 useEffect
'use client';
import { useState, useEffect } from 'react';
export default function DataList() {
  const [data, setData] = useState([]);
  useEffect(() => {
    fetch('/api/data').then(r => r.json()).then(setData);
  }, []);
  return <ul>{data.map(d => <li key={d.id}>{d.name}</li>)}</ul>;
}
```

### 5. 布局和元数据
```jsx
// app/layout.js
export const metadata = {
  title: 'My App',
  description: 'A Next.js application',
};

export default function RootLayout({ children }) {
  return (
    <html lang="zh">
      <body>{children}</body>
    </html>
  );
}
```
- `metadata` 对象自动生成 `<head>` 标签
- 每个页面可覆盖 metadata

### 6. 样式
- 默认支持 CSS Modules：`styles.module.css`
- 全局样式：`globals.css` 在 `layout.js` 中导入
- Tailwind CSS：如 `next.config.js` 配置了，直接使用 class
- **禁止** 在 Server Component 中使用 CSS-in-JS（styled-components 等）

### 7. 错误处理
```jsx
// app/error.js（必须是 Client Component）
'use client';
export default function Error({ error, reset }) {
  return (
    <div>
      <h2>出错了: {error.message}</h2>
      <button onClick={() => reset()}>重试</button>
    </div>
  );
}

// app/not-found.js
export default function NotFound() {
  return <h2>页面不存在</h2>;
}
```

### 8. 环境变量
- 服务端变量：`.env.local` 中的 `DB_URL=xxx`
- 客户端变量：必须 `NEXT_PUBLIC_` 前缀：`NEXT_PUBLIC_API_URL=xxx`
- **禁止** 在客户端代码中直接使用非 `NEXT_PUBLIC_` 变量
