import re
from collections import Counter
from typing import Dict, List, Optional


def read_file(file_path: str) -> Optional[str]:
    """
    读取文件内容
    
    Args:
        file_path: 文件路径
        
    Returns:
        文件内容字符串，如果出错则返回None
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
        return content
    except FileNotFoundError:
        print(f"错误: 文件 {file_path} 不存在")
        return None
    except UnicodeDecodeError:
        print(f"错误: 文件 {file_path} 编码格式不支持，请使用UTF-8编码")
        return None
    except Exception as e:
        print(f"读取文件时发生未知错误: {e}")
        return None


def tokenize(text: str) -> List[str]:
    """
    对文本进行分词处理
    
    Args:
        text: 输入文本
        
    Returns:
        分词后的单词列表
    """
    if not text:
        return []
    
    # 使用正则表达式提取单词，只保留字母数字下划线组成的词
    tokens = re.findall(r'\b\w+\b', text.lower())
    return tokens


def count_words(tokens: List[str]) -> Dict[str, int]:
    """
    统计词频
    
    Args:
        tokens: 分词列表
        
    Returns:
        词频字典
    """
    if not tokens:
        return {}
    
    word_count = Counter(tokens)
    return dict(word_count)


def process_file(file_path: str) -> Dict[str, int]:
    """
    处理文件的完整流程：读取文件 -> 分词 -> 统计词频
    
    Args:
        file_path: 文件路径
        
    Returns:
        词频字典
    """
    # 读取文件
    content = read_file(file_path)
    if content is None:
        return {}
    
    # 分词
    tokens = tokenize(content)
    
    # 统计词频
    word_freq = count_words(tokens)
    
    return word_freq


def get_top_n_words(word_freq: Dict[str, int], n: int = 10) -> List[tuple]:
    """
    获取出现频率最高的n个词
    
    Args:
        word_freq: 词频字典
        n: 返回前n个高频词，默认为10
        
    Returns:
        包含(词, 频次)元组的列表
    """
    if not word_freq:
        return []
    
    # 按照频次降序排列
    sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
    return sorted_words[:n]