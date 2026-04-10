# Django 后端编码规范

## 项目结构
```
project_name/
  manage.py             # Django CLI 入口
  project_name/
    __init__.py
    settings.py          # 全局配置
    urls.py              # 根 URL 路由
    wsgi.py              # WSGI 入口
  app_name/
    __init__.py
    models.py            # 数据模型
    views.py             # 视图函数/类视图
    urls.py              # App 级 URL 路由
    serializers.py       # DRF 序列化器（如使用 REST framework）
    admin.py             # Admin 注册
    apps.py              # App 配置
```

## 核心规则

### 1. 项目初始化
- 使用 `django-admin startproject project_name .`（注意末尾的点）
- 使用 `python manage.py startapp app_name` 创建应用
- **必须** 在 `settings.py` 的 `INSTALLED_APPS` 中注册应用

### 2. 模型定义
```python
from django.db import models

class User(models.Model):
    username = models.CharField(max_length=50, unique=True)
    email = models.EmailField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.username

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
```
- 每个模型必须有 `__str__` 方法
- 必须有 `to_dict()` 方法（或使用 DRF Serializer）
- `auto_now_add=True` 用于创建时间，`auto_now=True` 用于更新时间
- 🚨 **外键级联铁律**：所有 `models.ForeignKey` 必须明确指定 `on_delete` 参数（通常为 `models.CASCADE`），否则迁移直接报错！

### 3. URL 路由
```python
# project/urls.py
from django.urls import path, include
urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('app_name.urls')),
]

# app_name/urls.py
from django.urls import path
from . import views
urlpatterns = [
    path('users/', views.user_list, name='user-list'),
    path('users/<int:pk>/', views.user_detail, name='user-detail'),
]
```
- App 内部使用相对导入 `from . import views`（Django 项目例外，允许相对导入）
- 使用 `include()` 分层路由
- 🚨 **Trailing Slash (末尾斜杠) 铁律**：
  - Django 路由默认要求末尾有 `/`（如 `path('users/', ...)`）。
  - **如果前端 `fetch('/api/users')` 漏了末尾斜杠，Django 会触发 301 重定向到 `/api/users/`，导致 POST 请求被浏览器强制转为 GET 并丢弃 Body！**最终引发毫无头绪的 CORS 错误或 405 错误。
  - **前后端必须严格对齐：要么全带 `/`，要么全不带！推荐前端一律加 `/`**。

### 4. 视图函数（Function-Based Views）
```python
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json

@csrf_exempt
def user_list(request):
    if request.method == 'GET':
        users = User.objects.all()
        return JsonResponse([u.to_dict() for u in users], safe=False)
    elif request.method == 'POST':
        data = json.loads(request.body)
        user = User.objects.create(**data)
        return JsonResponse(user.to_dict(), status=201)
```
- API 端点必须加 `@csrf_exempt`（或配置 CSRF 中间件豁免）
- 返回列表时 `JsonResponse(list, safe=False)`（safe=False 必须）
- 解析 POST body：`json.loads(request.body)`
- 🚨 **传统 SSR 表单的 CSRF 403 铁律**：
  - 如果不使用 `@csrf_exempt` 而是渲染 HTML 表单提给后端，**`<form>` 内部必须包含 `{% csrf_token %}`**，否则提交 100% 报 `403 Forbidden`！

### 5. Django REST Framework（DRF）模式
```python
# serializers.py
from rest_framework import serializers

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = '__all__'

# views.py
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(['GET', 'POST'])
def user_list(request):
    if request.method == 'GET':
        users = User.objects.all()
        serializer = UserSerializer(users, many=True)
        return Response(serializer.data)
    elif request.method == 'POST':
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)
```

### 6. Settings 配置要点
```python
# CORS（django-cors-headers）
INSTALLED_APPS += ['corsheaders']
MIDDLEWARE.insert(0, 'corsheaders.middleware.CorsMiddleware')
CORS_ALLOW_ALL_ORIGINS = True  # 开发环境

# 数据库（默认 SQLite）
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# REST Framework（如使用 DRF）
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
}
```

### 7. 数据库迁移
```bash
python manage.py makemigrations
python manage.py migrate
```
- **修改模型后必须执行迁移**
- ASTrea 环境下在 main 入口自动调用：
```python
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project_name.settings')
django.setup()
from django.core.management import call_command
call_command('migrate', '--run-syncdb')
```

### 8. 启动
```python
if __name__ == "__main__":
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project_name.settings')
    from django.core.management import execute_from_command_line
    execute_from_command_line(['manage.py', 'runserver', '0.0.0.0:5001'])
```
- 端口必须与 api_contracts 一致
- **禁止 8000 端口**
