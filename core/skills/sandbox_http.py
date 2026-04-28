"""
SandboxHttpSkill — QA 特化 HTTP 请求技能

从原 SkillRunner._skill_http_request 原样提取。
安全护栏：只允许请求 localhost (127.0.0.1 / localhost / 0.0.0.0)

⚠️ 这是 QA 特化 Skill，不可直接公共化（PM 需要广域网访问能力）。
"""
import json
import logging
from urllib.parse import urlparse, quote

from core.skills.base import BaseSkill

logger = logging.getLogger("SkillRunner")

HTTP_TIMEOUT = 10


class SandboxHttpSkill(BaseSkill):
    """沙盒 HTTP 请求 — 仅限 localhost，QA Agent 专属"""

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "http_request",
                "description": (
                    "向本地运行的服务发送 HTTP 请求。"
                    "用于验证 API 端点是否正常工作。只能请求 localhost。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                            "description": "HTTP 方法"
                        },
                        "url": {
                            "type": "string",
                            "description": "完整 URL（如 'http://127.0.0.1:5001/api/items'）"
                        },
                        "body": {
                            "type": "object",
                            "description": "请求体（JSON 格式，用于 POST/PUT）"
                        },
                        "headers": {
                            "type": "object",
                            "description": "自定义请求头"
                        },
                    },
                    "required": ["method", "url"],
                },
            },
        }

    def execute(self, **kwargs) -> str:
        method = kwargs["method"]
        url = kwargs["url"]
        body = kwargs.get("body")
        headers = kwargs.get("headers")

        import urllib.request
        import urllib.error

        # 安全护栏: 只允许 localhost
        parsed = urlparse(url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "0.0.0.0"):
            return f"错误: 只允许请求 localhost，不允许 '{parsed.hostname}'"

        try:
            req_headers = {"Content-Type": "application/json"}
            if headers:
                # 忽略大小写替换 Content-Type
                for k, v in headers.items():
                    if k.lower() == "content-type":
                        req_headers["Content-Type"] = v
                    else:
                        req_headers[k] = v

            data = None
            if body and method.upper() in ("POST", "PUT", "PATCH"):
                if "application/x-www-form-urlencoded" in req_headers["Content-Type"].lower():
                    from urllib.parse import urlencode
                    data = urlencode(body).encode("utf-8")
                else:
                    data = json.dumps(body).encode("utf-8")

            # 对 URL 中的非 ASCII 字符做 percent-encoding（如中文路径 /category/餐饮）
            safe_url = url
            try:
                p = urlparse(url)
                encoded_path = quote(p.path, safe='/:@!$&\'()*+,;=-._~')
                encoded_query = quote(p.query, safe='/:@!$&\'()*+,;=-._~?=') if p.query else ''
                safe_url = f"{p.scheme}://{p.netloc}{encoded_path}"
                if encoded_query:
                    safe_url += f"?{encoded_query}"
                if p.fragment:
                    safe_url += f"#{p.fragment}"
            except Exception:
                safe_url = url  # 编码失败时保持原 URL

            req = urllib.request.Request(
                safe_url, data=data, headers=req_headers, method=method.upper()
            )

            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                status = resp.status
                resp_body = resp.read().decode("utf-8", errors="replace")[:500]
                return f"HTTP {status} OK\n{resp_body}"

        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                raw_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                raw_body = ""

            # 结构化提取：如果响应是 HTML 错误页面，提取关键信息
            if raw_body and ("<html" in raw_body.lower() or "<!doctype" in raw_body.lower()):
                body_text = _extract_error_from_html(raw_body)
            else:
                body_text = raw_body[:500]

            return f"⚠️ HTTP {e.code} {e.reason} (非200响应！)\n{body_text}"
        except urllib.error.URLError as e:
            return f"连接失败: {e.reason}"
        except TimeoutError:
            return f"请求超时 ({HTTP_TIMEOUT}s)"
        except Exception as e:
            return f"请求异常: {type(e).__name__}: {e}"


def _extract_error_from_html(html: str) -> str:
    """从 Flask/Werkzeug 500 错误页面 HTML 中提取结构化错误摘要。

    Flask debugger 页面结构：
      <title>jinja2.exceptions.UndefinedError: 'total_all' is undefined</title>
      <h1>...</h1>
      <div class="traceback">...</div>

    返回格式：
      [错误类型] 错误消息
      [Traceback 末行] File "routes.py", line 42, in index
    """
    import re as _re

    parts = []

    # 1. 从 <title> 提取异常类型和消息（最可靠）
    title_match = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
    if title_match:
        title_text = title_match.group(1).strip()
        # 清理 HTML 实体
        title_text = title_text.replace("&#39;", "'").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#34;", '"')
        if title_text and title_text.lower() not in ("error", "server error", "internal server error"):
            parts.append(f"[异常] {title_text}")

    # 2. 从 Werkzeug traceback 提取最后一个 File "xxx" 行
    file_matches = _re.findall(
        r'File\s+["\']([^"\']+)["\'],\s+line\s+(\d+),\s+in\s+(\w+)',
        html,
    )
    if file_matches:
        last = file_matches[-1]
        # 只保留文件名（去掉绝对路径）
        fname = last[0].replace("\\", "/").split("/")[-1]
        parts.append(f"[位置] {fname}:L{last[1]} in {last[2]}")

    # 3. 提取 Werkzeug 的 exception value（<blockquote> 或 <p class="errormsg">）
    for pattern in [
        r'<blockquote[^>]*>(.*?)</blockquote>',
        r'class="errormsg[^"]*"[^>]*>(.*?)</p>',
        r'<h1[^>]*>(.*?)</h1>',
    ]:
        m = _re.search(pattern, html, _re.IGNORECASE | _re.DOTALL)
        if m:
            text = _re.sub(r'<[^>]+>', '', m.group(1)).strip()
            text = text.replace("&#39;", "'").replace("&amp;", "&")
            if text and len(text) < 300 and text not in [p.split("] ", 1)[-1] for p in parts]:
                parts.append(f"[详情] {text}")
                break

    if parts:
        return "\n".join(parts)

    # 兜底: 去除 HTML 标签，取前 400 字符
    plain = _re.sub(r'<[^>]+>', ' ', html)
    plain = _re.sub(r'\s+', ' ', plain).strip()
    return plain[:400]
