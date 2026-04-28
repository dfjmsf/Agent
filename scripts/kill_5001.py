import os
import subprocess
import re
import sys
import argparse

def get_pids_for_port(port: int) -> list[int]:
    """使用 netstat 查找占用指定端口的 PID"""
    pids = []
    try:
        # 执行 netstat -ano
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True,
            check=True
        )
        
        # Windows GBK default, decode safely
        output = result.stdout.decode('gbk', errors='ignore')
        
        # 寻找匹配的行: TCP    127.0.0.1:5001     0.0.0.0:0      LISTENING       12345
        pattern = re.compile(rf'TCP\s+[\d\.]*:{port}\s+.*?\s+(\d+)')
        
        for line in output.splitlines():
            match = pattern.search(line)
            if match:
                pid = int(match.group(1))
                if pid > 0 and pid not in pids:
                    pids.append(pid)
    except Exception as e:
        print(f"执行 netstat 失败: {e}")
        
    return pids

def kill_process(pid: int):
    """使用 taskkill 强制结束进程"""
    print(f"[*] 正在结束进程 PID: {pid}...")
    try:
        subprocess.run(
            ['taskkill', '/F', '/PID', str(pid)],
            check=True,
            capture_output=True
        )
        print(f"[+] 成功清理 PID: {pid}")
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode('gbk', errors='ignore').strip() or e.stdout.decode('gbk', errors='ignore').strip()
        print(f"[!] 无法结束进程 {pid}: {err}")
    except Exception as e:
        print(f"[-] 未知错误 (PID {pid}): {e}")

def main():
    parser = argparse.ArgumentParser(description="一键清理指定端口占用的幽灵进程")
    parser.add_argument("-p", "--port", type=int, default=5001, help="需要清理的端口号 (默认: 5001)")
    args = parser.parse_args()

    port = args.port
    print(f"[*] 正在扫描占用端口 {port} 的进程...")
    
    pids = get_pids_for_port(port)
    
    if not pids:
        print(f"[*] 端口 {port} 当前没有任何进程占用，状态安全。")
        return
    
    print(f"[!] 发现 {len(pids)} 个进程占用端口 {port}: {pids}")
    
    for pid in pids:
        kill_process(pid)

if __name__ == "__main__":
    main()
