"""
Package Map — 静态映射表
提供 import 名 → pip 包名的映射关系，以及 Python 3.11 标准库模块列表。
用于 sandbox 自动扫描 import 并安装缺失的第三方包。
"""

# import 名 → pip 包名（仅列异名包，同名包自动 fallback 到 import 名本身）
IMPORT_TO_PACKAGE = {
    # 图像 & 视觉
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "skimage": "scikit-image",
    
    # 数据科学
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    
    # Web
    "dotenv": "python-dotenv",
    "jose": "python-jose",
    "jwt": "PyJWT",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
    
    # 加密 & 安全
    "Crypto": "pycryptodome",
    "nacl": "PyNaCl",
    "passlib": "passlib",
    
    # 文档 & Office
    "docx": "python-docx",
    "pptx": "python-pptx",
    "openpyxl": "openpyxl",
    "xlrd": "xlrd",
    
    # 数据库
    "pymongo": "pymongo",
    "motor": "motor",
    "psycopg2": "psycopg2-binary",
    "MySQLdb": "mysqlclient",
    "pymysql": "PyMySQL",
    
    # 工具类
    "serial": "pyserial",
    "usb": "pyusb",
    "magic": "python-magic",
    "dateutil": "python-dateutil",
    "tz": "pytz",
    "attr": "attrs",
    "pydantic": "pydantic",
    "tqdm": "tqdm",
    "rich": "rich",
    "click": "click",
    "typer": "typer",
    "colorama": "colorama",
    "tabulate": "tabulate",
    "wx": "wxPython",
}

# Python 3.11 标准库模块（不需要 pip install）
# 参考：https://docs.python.org/3.11/py-modindex.html
STDLIB_MODULES = {
    # 内置类型 & 常量
    "builtins", "sys", "os", "io", "abc",
    
    # 编译器 & AST
    "ast", "token", "tokenize", "keyword", "symtable", "compileall",
    "code", "codeop",
    
    # 文本处理
    "string", "re", "difflib", "textwrap", "unicodedata", "readline",
    
    # 数据类型
    "datetime", "zoneinfo", "calendar", "collections", "heapq", "bisect",
    "array", "weakref", "types", "copy", "pprint", "reprlib", "enum",
    "graphlib", "dataclasses", "contextlib", "decimal", "fractions",
    "numbers", "math", "cmath", "statistics", "random",
    
    # 函数式编程
    "itertools", "functools", "operator",
    
    # 文件 & 目录
    "pathlib", "fileinput", "stat", "filecmp", "tempfile", "glob",
    "fnmatch", "linecache", "shutil",
    
    # 数据序列化
    "pickle", "shelve", "marshal", "dbm", "sqlite3", "csv",
    "configparser", "tomllib", "json",

    # 压缩 & 归档
    "zlib", "gzip", "bz2", "lzma", "zipfile", "tarfile",
    
    # 加密
    "hashlib", "hmac", "secrets",
    
    # 操作系统
    "os", "platform", "errno", "ctypes", "subprocess", "sysconfig",
    "signal", "mmap",
    
    # 并发
    "threading", "multiprocessing", "concurrent", "queue", "sched",
    "asyncio",
    
    # 网络
    "socket", "ssl", "select", "selectors", "asyncore", "asynchat",
    "http", "urllib", "ftplib", "poplib", "imaplib", "smtplib",
    "email", "mailbox", "mimetypes", "base64", "binascii",
    "quopri", "uu",
    
    # HTML & XML
    "html", "xml",
    
    # Internet 协议
    "webbrowser", "xmlrpc", "ipaddress",
    
    # 结构化标记
    "struct", "codecs",
    
    # GUI (自带)
    "tkinter",
    
    # 开发工具
    "typing", "pydoc", "doctest", "unittest", "test",
    
    # 调试 & 分析
    "bdb", "pdb", "timeit", "trace", "tracemalloc", "traceback",
    "logging", "warnings",
    
    # 运行时
    "atexit", "gc", "inspect", "dis", "importlib", "pkgutil",
    
    # 杂项
    "argparse", "getopt", "gettext", "locale", "time", "uuid",
    "getpass", "curses", "readline", "rlcompleter",
}

# 常见项目内模块名（几乎不可能是 PyPI 包，直接跳过）
COMMON_PROJECT_MODULES = {
    "app", "api", "main", "config", "settings", "utils", "helpers",
    "models", "schemas", "routes", "views", "controllers", "services",
    "middlewares", "middleware", "database", "db", "tests", "test",
    "core", "lib", "common", "shared", "base", "server", "client",
    "frontend", "backend", "static", "templates", "migrations",
    "handlers", "events", "tasks", "workers", "routers", "router",
    "exceptions", "errors", "auth", "admin", "manage",
    "setup", "run", "start", "index", "init",
}
