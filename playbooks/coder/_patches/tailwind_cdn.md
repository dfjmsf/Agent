# Tailwind CSS CDN 补丁（Addon）

## CDN 引入规则

1. **HTML 必须引入 Tailwind CSS CDN**
   - 在 `<head>` 中引入 Tailwind Play CDN：
   ```html
   <head>
       <script src="https://cdn.tailwindcss.com"></script>
   </head>
   ```
   - 禁止遗漏此引用，否则所有 Tailwind class 将不生效！

2. **样式优先使用 Tailwind class，而非 style.css**
   - 布局、间距、颜色、圆角、阴影等样式优先使用 Tailwind 工具类（如 `flex`, `p-4`, `bg-white`, `rounded-lg`, `shadow-md`）。
   - `style.css` 仅用于 Tailwind 无法覆盖的自定义样式（如动画、渐变、特殊伪元素）。
   - 禁止在 style.css 中写 `@tailwind base;` 或 `@apply`（CDN 模式不支持 PostCSS 指令）。

3. **常用 Tailwind 模式参考**
   - 卡片：`class="bg-white rounded-lg shadow-md p-6"`
   - 按钮：`class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded"`
   - 输入框：`class="w-full border border-gray-300 rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"`
   - 两列布局：`class="grid grid-cols-2 gap-6"`
   - 居中容器：`class="max-w-6xl mx-auto px-4"`
