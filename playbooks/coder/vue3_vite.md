# Vue 3 Vite 编码规范（SFC 模式）

## 项目结构规范

Vite + Vue 3 项目必须包含以下核心文件：

```
package.json          → 项目依赖配置
vite.config.js        → Vite 构建配置
index.html            → 入口 HTML（项目根目录，非 public/）
src/
├── main.js           → Vue 应用入口
├── App.vue           → 根组件
├── components/       → 子组件目录（按需）
└── style.css         → 全局样式（按需）
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
    "vue": "^3.4.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "vite": "^5.0.0"
  }
}
```

- **dependencies** 中只列出运行时必须的包（如 vue, axios, pinia）
- **devDependencies** 中列出构建工具（如 vite, @vitejs/plugin-vue）
- 禁止遗漏 `"type": "module"`（Vite 要求 ESM）

### 2. vite.config.js

```javascript
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
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

- **必须配置 `/api` 代理**指向后端端口（规划书中的 base_url 端口）
- 这样前端 `fetch('/api/xxx')` 在开发模式下也能正确访问后端

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
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
</body>
</html>
```

- **index.html 必须在项目根目录**，不是 public/
- `<script type="module">` 是 Vite 必须的
- 不需要引入 Vue CDN（由 npm 管理）

### 4. src/main.js

```javascript
import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

createApp(App).mount('#app')
```

### 5. src/App.vue（SFC 单文件组件）

使用 `<script setup>` 语法糖（Composition API 最简写法）：

```vue
<template>
  <div class="container">
    <h1>{{ title }}</h1>
    <div v-for="item in items" :key="item.id">
      {{ item.name }}
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'

const title = ref('My App')
const items = ref([])

const loadItems = async () => {
  try {
    const res = await fetch('/api/items')
    const data = await res.json()
    items.value = data.items || []
  } catch (error) {
    console.error('加载失败:', error)
  }
}

onMounted(() => {
  loadItems()
})
</script>

<style scoped>
.container {
  max-width: 800px;
  margin: 0 auto;
  padding: 20px;
}
</style>
```

### 6. 关键注意事项

- **禁止使用 CDN 引入 Vue**：Vite 项目通过 npm 管理 Vue，不需要也不能用 CDN
- **API 请求使用相对路径**：`fetch('/api/xxx')`，Vite dev server 会通过 proxy 转发
- **组件文件名大驼峰**：`UserList.vue`、`MealForm.vue`
- **CSS 可用 scoped**：`<style scoped>` 限制样式只作用于当前组件
- **构建产物在 dist/**：`npm run build` 后生成 `dist/index.html` + `dist/assets/`

### 7. 🚨 子组件向父组件传值：必须用 emit，严禁用 props 回调

> **这是致命铁律，违反将导致按钮点击无反应！**

**✅ 正确写法（Vue3 emit 模式）：**

子组件 `RecipeForm.vue`：
```vue
<script setup>
const emit = defineEmits(['add'])

const handleSubmit = () => {
  emit('add', { name: form.name, time: form.time })
  // 重置表单...
}
</script>
```

父组件 `App.vue`：
```vue
<RecipeForm @add="addRecipe" />
```

**❌ 严禁写法（React 的 onXxx 回调模式）：**
```vue
<!-- 严禁这样写！这是 React 模式，在 Vue3 中会导致 onAdd is undefined 错误 -->
<script setup>
defineProps({ onAdd: { type: Function } })

const handleSubmit = () => {
  onAdd(formData)  // ← 崩溃！onAdd 未定义
}
</script>
```

**规则总结：**
- 子 → 父通信 **只用** `defineEmits` + `emit('事件名', 数据)`
- 父组件用 `@事件名="处理函数"` 监听
- **严禁** `defineProps({ onXxx: Function })`，这是 React 写法
- `defineProps` 只用于父 → 子的数据传递（如 `:recipe="item"`）

