import re


def validate_input(input_str, input_type="number"):
    """
    验证用户输入是否符合要求
    :param input_str: 输入的字符串
    :param input_type: 输入类型，"number" 表示数字，"string" 表示字符串
    :return: 验证结果和处理后的值
    """
    try:
        if input_type == "number":
            # 检查是否为有效数字
            value = int(input_str.strip())
            return True, value
        elif input_type == "string":
            # 检查字符串是否为空或仅包含空白字符
            if not input_str or input_str.strip() == "":
                return False, None
            return True, input_str.strip()
        else:
            return False, None
    except (ValueError, TypeError):
        return False, None


def check_number_range(num, min_val, max_val):
    """
    检查数字是否在指定范围内
    :param num: 要检查的数字
    :param min_val: 最小值
    :param max_val: 最大值
    :return: 是否在范围内
    """
    try:
        num = int(num)
        return min_val <= num <= max_val
    except (ValueError, TypeError):
        return False


def sanitize_string(input_str):
    """
    清理字符串，移除潜在的危险字符
    :param input_str: 输入字符串
    :return: 清理后的字符串
    """
    try:
        # 移除控制字符和潜在危险字符
        sanitized = re.sub(r'[^\w\s\u4e00-\u9fff]', '', input_str)
        return sanitized.strip()
    except Exception:
        return ""


def is_valid_integer(value):
    """
    检查值是否为有效的整数
    :param value: 待检查的值
    :return: 是否为有效整数
    """
    try:
        int(value)
        return True
    except (ValueError, TypeError):
        return False


def format_error_message(error_msg):
    """
    格式化错误消息
    :param error_msg: 原始错误消息
    :return: 格式化后的错误消息
    """
    try:
        if not error_msg:
            return "未知错误"
        return str(error_msg).strip()
    except Exception:
        return "格式化错误"