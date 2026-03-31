# Vue 3 Composition API 补丁（Addon）

## 覆盖规则

本补丁覆盖 Vue3 CDN 基础规范中的 Options API 写法，改为 Composition API 模式。

1. **使用 `setup()` + `ref()`/`reactive()` 代替 `data()`**
   ```javascript
   const { createApp, ref, reactive, onMounted } = Vue;

   const app = createApp({
       setup() {
           const items = ref([]);
           const loading = ref(false);
           const form = reactive({
               title: '',
               content: ''
           });

           const loadItems = async () => {
               loading.value = true;
               try {
                   const res = await fetch('/api/items');
                   const data = await res.json();
                   items.value = data.items || [];
               } catch (error) {
                   console.error('加载失败:', error);
               } finally {
                   loading.value = false;
               }
           };

           const saveItem = async () => {
               try {
                   const res = await fetch('/api/items', {
                       method: 'POST',
                       headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify({ ...form })
                   });
                   if (res.ok) {
                       form.title = '';
                       form.content = '';
                       await loadItems();
                   }
               } catch (error) {
                   console.error('保存失败:', error);
               }
           };

           onMounted(() => {
               loadItems();
           });

           return { items, loading, form, loadItems, saveItem };
       }
   });
   app.mount('#app');
   ```

2. **关键注意事项**
   - `ref()` 的值在 JS 中通过 `.value` 访问，但在模板中自动解包（不需要 `.value`）。
   - `reactive()` 用于对象/表单，直接通过属性访问（如 `form.title`）。
   - 从 `Vue` 全局对象中解构所需的 API：`const { createApp, ref, reactive, onMounted, computed } = Vue;`
   - 函数直接定义在 `setup()` 中，通过 return 暴露给模板。
   - 生命周期钩子使用函数形式：`onMounted(() => {...})` 代替 `mounted() {...}`。

3. **HTML 模板语法不变**
   - 模板中仍然使用 `v-model`、`v-for`、`@click`、`@submit.prevent` 等指令，这些与 Options API 完全一致。
