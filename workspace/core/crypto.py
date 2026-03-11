"""
凯撒密码加密和解密模块

提供凯撒密码的加密和解密功能，支持大小写字母，
忽略非字母字符，并包含完整的输入验证与异常处理。
"""

def caesar_encrypt(text: str, offset: int) -> str:
    """
    凯撒密码加密函数
    
    Args:
        text: 待加密的文本
        offset: 偏移量
        
    Returns:
        加密后的文本
        
    Raises:
        TypeError: 当输入参数类型不正确时抛出
        ValueError: 当偏移量超出合理范围时抛出
    """
    # 输入验证
    if not isinstance(text, str):
        raise TypeError(f"文本参数必须为字符串类型，当前类型为 {type(text).__name__}")
    
    if not isinstance(offset, int):
        raise TypeError(f"偏移量必须为整数类型，当前类型为 {type(offset).__name__}")
    
    # 标准化偏移量到0-25范围内
    offset = offset % 26
    
    result = []
    
    for char in text:
        if char.isalpha():
            # 确定字符是大写还是小写
            base = ord('A') if char.isupper() else ord('a')
            # 执行凯撒加密变换
            encrypted_char = chr((ord(char) - base + offset) % 26 + base)
            result.append(encrypted_char)
        else:
            # 非字母字符保持不变
            result.append(char)
    
    return ''.join(result)


def caesar_decrypt(text: str, offset: int) -> str:
    """
    凯撒密码解密函数
    
    Args:
        text: 待解密的文本
        offset: 加密时使用的偏移量
        
    Returns:
        解密后的文本
        
    Raises:
        TypeError: 当输入参数类型不正确时抛出
        ValueError: 当偏移量超出合理范围时抛出
    """
    # 输入验证
    if not isinstance(text, str):
        raise TypeError(f"文本参数必须为字符串类型，当前类型为 {type(text).__name__}")
    
    if not isinstance(offset, int):
        raise TypeError(f"偏移量必须为整数类型，当前类型为 {type(offset).__name__}")
    
    # 解密相当于用负偏移量进行加密
    return caesar_encrypt(text, -offset)


def validate_caesar_input(text: str, offset: int) -> bool:
    """
    验证凯撒密码输入参数的有效性
    
    Args:
        text: 待处理的文本
        offset: 偏移量
        
    Returns:
        参数是否有效
    """
    try:
        # 尝试调用加密函数以验证输入
        caesar_encrypt(text, offset)
        return True
    except (TypeError, ValueError):
        return False