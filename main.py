import os
import sys
import argparse
import logging

# 确保能找到 core 和 agents 模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agents.manager import ManagerAgent

# 设置基础的控制台日志输出格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)-15s | %(levelname)-7s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("MultiAgentCLI")

def read_prompt_from_file(file_path: str) -> str:
    """从外部文件中读取长篇需求"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"读取需求文件失败: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="🚀 Qwen Multi-Agent 协同代码生成框架",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
【示例用法】
  直接输入提示词:
    python main.py --prompt "写一个带 sqlite 的 fastapi 博客后端"
    
  从本地文件读取提示词 (适合长需求/PRD):
    python main.py --file my_requirements.txt
    
  指定特定的输出目录:
    python main.py --prompt "写个爬虫" --out_dir "data/my_spider_project"
        """
    )
    
    # 互斥参数组：必须提供 prompt 或 file 其中的一个
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-p', '--prompt', type=str, help='一句话项目需求')
    group.add_argument('-f', '--file', type=str, help='包含具体项目需求文本的文件路径')
    
    parser.add_argument('-o', '--out_dir', type=str, help='项目产出目录。如果不指定，将自动用时间戳在 ./projects 目录下创建。')
    
    args = parser.parse_args()

    # 1. 提取真实项目需求内容
    user_requirement = ""
    if args.file:
        logger.info(f"正在从文件加载需求: {args.file}")
        user_requirement = read_prompt_from_file(args.file)
    else:
        user_requirement = args.prompt

    if not user_requirement.strip():
        logger.error("❌ 错误：为您提供的项目需求是空的！")
        sys.exit(1)

    # 2. 如果存在上一个项目残留，强行清空历史幽灵代码
    # (已由 Manager.run_project() 内部统一处理)

    print("\n" + "="*60)
    print("▶️ 开始进行多智能体协同代码生成...")
    print("="*60 + "\n")

    manager = ManagerAgent()
    success, final_dir = manager.run_project(user_requirement, args.out_dir)

    if not success:
        logger.critical("💥 项目生成失败，请查看上方日志获取详细信息。")
        sys.exit(1)

    logger.info(f"✨ 项目已完美封装于: {final_dir}")

if __name__ == "__main__":
    main()
