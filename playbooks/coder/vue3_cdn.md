# Vue 3 CDN 前端编码规范

## CDN 引入规则

1. **HTML 必须引入 Vue 3 CDN**
   - 在 `</body>` 之前、应用脚本之前引入 Vue：
   ```html
   <!-- Vue 3 CDN 必须在 app.js 之前加载 -->
   <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
   <script src="./app.js"></script>
   ```
   - 禁止遗漏 Vue CDN 引用，否则 `Vue is not defined` 会导致整个前端崩溃！

2. **HTML 必须包含 Vue 挂载点**
   ```html
   <!DOCTYPE html>
   <html lang="zh-CN">
   <head>
       <meta charset="UTF-8">
       <meta name="viewport" content="width=device-width, initial-scale=1.0">
       <title>页面标题</title>
       <link rel="stylesheet" href="./style.css">
   </head>
   <body>
       <div id="app">
           <!-- Vue 模板内容写在这里 -->
       </div>
       <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
       <script src="./app.js"></script>
   </body>
   </html>
   ```

## Vue 3 应用规则

3. **使用 Vue.createApp 创建应用**
   ```javascript
   const app = Vue.createApp({
       data() {
           return {
               items: [],
               loading: false
           };
       },
       mounted() {
           this.loadItems();
       },
       methods: {
           async loadItems() { ... },
           async saveItem() { ... }
       }
   });
   app.mount('#app');
   ```

4. **使用 Vue 模板语法进行数据绑定**
   - 使用 `v-model` 实现表单双向绑定，禁止使用 `getElementById` + `value` 手动操作！
   - 使用 `@click`、`@submit.prevent` 绑定事件，禁止使用 `addEventListener`！
   - 使用 `v-for` 渲染列表，禁止手动拼接 `innerHTML`！
   - 使用 `v-if` / `v-show` 控制显隐，禁止手动操作 `style.display`！
   
   **正确示例（HTML 模板）：**
   ```html
   <div id="app">
       <form @submit.prevent="saveItem">
           <input v-model="title" placeholder="标题">
           <textarea v-model="content"></textarea>
           <button type="submit">保存</button>
       </form>
       <ul>
           <li v-for="item in items" :key="item.id">
               {{ item.title }}
               <button @click="deleteItem(item.id)">删除</button>
           </li>
       </ul>
   </div>
   ```

5. **JS 初始化方式**
   - Vue CDN 模式不需要 `DOMContentLoaded`，`app.mount('#app')` 本身就是初始化。
   - 整个 app.js 文件的结构应该是：
   ```javascript
   const app = Vue.createApp({ ... });
   app.mount('#app');
   ```
   - 禁止将 `Vue.createApp` 放在 `DOMContentLoaded` 回调里（除非有特殊需求），直接在脚本末尾调用即可。

6. **API 请求处理**
   - 在 `methods` 中定义异步方法，使用 `fetch` + `async/await`：
   ```javascript
   methods: {
       async loadItems() {
           try {
               this.loading = true;
               const res = await fetch('/api/items');
               const data = await res.json();
               this.items = data.items || [];
           } catch (error) {
               console.error('加载失败:', error);
               alert('加载失败: ' + error.message);
           } finally {
               this.loading = false;
           }
       }
   }
   ```

7. **禁止混用 Vue 和原生 DOM 操作**
   - 禁止在 Vue 应用中使用 `document.getElementById` 读写数据（表单值、列表渲染等），所有数据流必须走 Vue 的响应式系统。
   - 唯一例外：第三方库初始化（如图表库）可以用 `this.$refs` 或 `mounted` 中的 DOM 访问。
