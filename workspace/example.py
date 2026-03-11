import sys
import os

# 添加项目根目录到Python路径，以便导入其他模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 导入词频统计函数
from word_freq import word_frequency, get_top_words

def main():
    """
    主函数：演示词频统计功能
    """
    try:
        # 示例文本
        sample_text = """
        Python is a high-level programming language. Python is known for its simplicity and readability.
        Many developers choose Python for web development, data analysis, and machine learning.
        Python's syntax allows programmers to express concepts in fewer lines of code.
        """
        
        print("原始文本:")
        print(sample_text.strip())
        print("\n" + "="*50 + "\n")
        
        # 调用词频统计函数（不区分大小写）
        freq_result = word_frequency(sample_text, case_sensitive=False)
        print("词频统计结果（不区分大小写）:")
        for word, count in sorted(freq_result.items(), key=lambda x: x[1], reverse=True):
            print(f"{word}: {count}")
        
        print("\n" + "-"*30 + "\n")
        
        # 调用词频统计函数（区分大小写）
        freq_result_case_sensitive = word_frequency(sample_text, case_sensitive=True)
        print("词频统计结果（区分大小写）:")
        for word, count in sorted(freq_result_case_sensitive.items(), key=lambda x: x[1], reverse=True):
            print(f"{word}: {count}")
        
        print("\n" + "-"*30 + "\n")
        
        # 获取最高频的前5个词
        top_5_words = get_top_words(sample_text, n=5, case_sensitive=False)
        print("出现频率最高的前5个词:")
        for i, (word, count) in enumerate(top_5_words, 1):
            print(f"{i}. {word}: {count}")
            
    except Exception as e:
        print(f"执行过程中发生错误: {e}")

if __name__ == "__main__":
    main()