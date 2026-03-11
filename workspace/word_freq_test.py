
def count_words(text):
    """
    统计文本中每个单词出现的频率
    
    Args:
        text (str): 输入的文本字符串
        
    Returns:
        dict: 单词频率字典，键为单词，值为出现次数
    """
    try:
        if not isinstance(text, str):
            raise TypeError("输入必须是字符串类型")
        
        # 将文本转换为小写并分割成单词列表
        words = text.lower().split()
        
        # 创建字典存储单词频率
        word_freq = {}
        
        for word in words:
            # 去除标点符号
            clean_word = ''.join(char for char in word if char.isalnum())
            
            if clean_word:  # 确保不是空字符串
                if clean_word in word_freq:
                    word_freq[clean_word] += 1
                else:
                    word_freq[clean_word] = 1
        
        return word_freq
    
    except Exception as e:
        print(f"处理文本时发生错误: {e}")
        return {}


def main():
    """
    示例代码，演示如何使用 count_words 函数
    """
    # 示例文本
    sample_text = "Hello world! This is a sample text. Hello again, world."
    
    print("原始文本:")
    print(sample_text)
    print("\n单词频率统计结果:")
    
    # 调用 count_words 函数
    result = count_words(sample_text)
    
    # 输出结果
    for word, freq in sorted(result.items()):
        print(f"{word}: {freq}")
    
    # 额外测试用例
    print("\n--- 其他测试用例 ---")
    
    test_cases = [
        "",  # 空字符串
        "   ",  # 只有空格
        "One",  # 单个单词
        "Repeat repeat REPEAT",  # 重复单词不同大小写
        "Word with punctuation!!! And more... words?",  # 包含标点符号
    ]
    
    for i, test_text in enumerate(test_cases, 1):
        print(f"\n测试用例 {i}: '{test_text}'")
        result = count_words(test_text)
        print(f"结果: {result}")


if __name__ == "__main__":
    main()
