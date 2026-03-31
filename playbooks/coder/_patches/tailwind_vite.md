# Tailwind CSS Vite 构建模式补丁（Addon）

## 适用场景

当项目同时使用 **Vite 构建** + **Tailwind CSS** 时，使用此补丁代替 CDN 模式。

## 关键规则

### 1. 不使用 CDN！

Vite 项目中 **禁止** 在 index.html 中引入 `<script src="https://cdn.tailwindcss.com"></script>`。
Tailwind CSS 通过 npm 安装 + PostCSS 插件运行。

### 2. 🚨 package.json 必须包含完整 Tailwind 三件套

> **缺少任何一个都会导致 `npm run build` 失败：Cannot find module 'autoprefixer'！**

以下三个包必须全部放在 **devDependencies**（不是 dependencies）中：

```json
{
  "devDependencies": {
    "tailwindcss": "^3.3.0",
    "postcss": "^8.4.0",
    "autoprefixer": "^10.4.0"
  }
}
```

**❌ 常见错误：**
- 把 `tailwindcss` 放在 `dependencies` 而不是 `devDependencies`
- 遗漏 `postcss` 或 `autoprefixer`
- 三个包必须同时存在，缺一不可！


### 3. tailwind.config.js（ESM 格式）

```javascript
/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
```

### 4. postcss.config.js（⚠️ 必须用 ESM 格式！）

```javascript
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
```

> 🚨 **致命警告**：因为 package.json 中有 `"type": "module"`，所有 `.js` 文件默认按 ESM 解析。
> **禁止** 使用 `module.exports = {...}` 语法！否则 Vite build 会报错：
> `ReferenceError: module is not defined in ES module scope`

### 5. 全局 CSS 入口（src/style.css）

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

/* 自定义样式写在下方 */
```

- Vite 构建模式 **必须** 写 `@tailwind` 指令（与 CDN 模式相反！）
- 该文件需在 `src/main.js` 中 import

### 6. 常用 Tailwind 模式参考

- 卡片：`class="bg-white rounded-lg shadow-md p-6"`
- 按钮：`class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded"`
- 输入框：`class="w-full border border-gray-300 rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"`
- 网格布局：`class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"`
- 居中容器：`class="max-w-6xl mx-auto px-4"`
- 悬停缩放：`class="transition-transform hover:scale-105"`
