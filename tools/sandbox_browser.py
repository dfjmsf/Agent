import os
import base64
import logging
from typing import Tuple, Optional

logger = logging.getLogger("SandboxBrowser")

def take_screenshot(url: str, sandbox_dir: Optional[str] = None) -> Tuple[bool, str]:
    """
    启动无头 Chromium 浏览器，截取指定 URL 的屏幕快照。
    
    Args:
        url: 目标网页的完整 URL (包含协议头 http://)
        sandbox_dir: 工作区目录（预留作日后可能的下载沙盒化）

    Returns:
        (success, result_string)
        - 如果成功，result_string 是 Base64 编码的图像数据 (不含 data:前缀)
        - 如果失败，result_string 包含错误原因
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError
    except ImportError:
        logger.error("❌ Playwright 未安装。请执行: pip install playwright && playwright install chromium")
        return False, "Playwright library is missing. Please install it."

    try:
        with sync_playwright() as p:
            # 启动无头浏览器
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            # 设置一个现代的视口，尽量让设计完整展现
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=1,
            )
            page = context.new_page()

            # 设置整体超时
            page.set_default_timeout(15000)

            logger.info(f"🌐 [Browser] 正在访问: {url}")
            try:
                # domcontentloaded 代表 DOM 树已经构建完毕
                response = page.goto(url, wait_until="domcontentloaded")
                
                if not response or not response.ok:
                    status = response.status if response else "unknown"
                    logger.warning(f"⚠️ [Browser] 页面返回状态异常: {status}")
                    # 依然继续截图，因为 404 / 500 页面也是需要 UI 审核的
                    
                # 尝试等待网络空闲，但容忍超时，以防万一页面有持续的心跳轮询或大片骨架屏动画
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except TimeoutError:
                    pass
                    
            except Exception as e:
                browser.close()
                return False, f"Navigation failed: {str(e)}"

            logger.info(f"📸 [Browser] 正在对页面进行全屏截图...")
            # full_page=True 保证滚动截取长页面
            # type='jpeg', quality=80 能有效减小大图的 Base64 膨胀率，节约大模型 Token
            screenshot_bytes = page.screenshot(
                full_page=True, 
                type="jpeg", 
                quality=80
            )
            
            browser.close()

            encoded = base64.b64encode(screenshot_bytes).decode("utf-8")
            logger.info(f"✅ [Browser] 截图成功! 大小预估: {len(encoded) // 1024} KB")
            return True, encoded

    except Exception as e:
        logger.error(f"❌ [Browser] 浏览器执行异常: {e}")
        return False, f"Browser execution failed: {str(e)}"
