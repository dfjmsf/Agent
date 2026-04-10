# React Vite 编码规范（JSX 模式）

## 项目结构规范

Vite + React 项目必须包含以下核心文件：

```
package.json          → 项目依赖配置
vite.config.js        → Vite 构建配置
index.html            → 入口 HTML（项目根目录）
src/
├── main.jsx          → React 应用入口
├── App.jsx           → 根组件
├── App.css           → 根组件样式
├── components/       → 子组件目录（按需）
└── index.css         → 全局样式（按需）
```

## 文件编写规则

### 1. package.json（必须由 Coder 生成）

```json
{
  "name": "项目名（小写连字符）",
  "private": true,
  "version": "0.0.1",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.0",
    "vite": "^5.0.0"
  }
}
```

- **dependencies** 中只列出运行时必须的包（如 react, react-dom, axios）
- **devDependencies** 中列出构建工具（如 vite, @vitejs/plugin-react）
- 禁止遗漏 `"type": "module"`

### 2. vite.config.js

```javascript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:5001',
        changeOrigin: true
      }
    }
  }
})
```

- **必须配置 `/api` 代理**指向后端端口

### 3. index.html（根目录）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>项目标题</title>
</head>
<body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
</body>
</html>
```

- React 约定挂载点为 `<div id="root">`
- `<script type="module">` 是 Vite 必须的

### 4. src/main.jsx

```jsx
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
```

### 5. src/App.jsx（函数组件 + Hooks）

```jsx
import { useState, useEffect } from 'react'
import './App.css'

function App() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)

  const loadItems = async () => {
    setLoading(true)
    try {
      const res = await fetch('/api/items')
      const data = await res.json()
      setItems(data.items || [])
    } catch (error) {
      console.error('加载失败:', error)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadItems()
  }, [])

  return (
    <div className="container">
      <h1>My App</h1>
      {loading ? (
        <p>加载中...</p>
      ) : (
        items.map(item => (
          <div key={item.id}>{item.name}</div>
        ))
      )}
    </div>
  )
}

export default App
```

### 6. 关键注意事项

- **禁止使用 CDN 引入 React**：由 npm 管理
- **CSS 类名用 className**：JSX 中 `class` 是保留字，必须写 `className`
- **事件绑定驼峰命名**：`onClick`、`onChange`、`onSubmit`
- **API 请求使用相对路径**：`fetch('/api/xxx')`
- **表单提交**：在 `<form onSubmit={handleSubmit}>` 中调用 `e.preventDefault()`
- **列表渲染必须有 key**：`items.map(item => <div key={item.id}>...)</div>)`
- **状态更新不可变**：禁止直接 `items.push()`，必须 `setItems([...items, newItem])`
- **构建产物在 dist/**：`npm run build` 后生成 `dist/index.html` + `dist/assets/`

### 🚨 致命坑点防御：无限渲染死循环 (Infinite Loop)
> **Agent 最容易犯的毁灭性错误：组件无限发请求刷爆后端！**

- **严禁直接在组件主体内发请求**：`fetch()` 改变状态 -> 触发重渲染 -> 再次 `fetch()` -> 死循环！
- **`useEffect` 依赖数组铁律**：如果是只在挂载时获取一次数据，**必须**传入空数组 `[]`！
  ```jsx
  // ⛔ 致命错误：漏写 [] 会导致每次渲染都重新触发！
  useEffect(() => { loadItems() }) 
  
  // ✅ 标准安全阵法
  useEffect(() => { loadItems() }, []) 
  ```
