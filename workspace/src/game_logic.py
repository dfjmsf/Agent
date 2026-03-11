import random
from typing import Tuple


class NumberGuessingGame:
    """
    猜数字游戏核心逻辑类
    包含生成随机数、判断猜测结果等核心功能
    """

    def __init__(self, min_num: int = 1, max_num: int = 100, max_attempts: int = 10):
        """
        初始化游戏参数
        
        Args:
            min_num: 随机数最小值，默认为1
            max_num: 随机数最大值，默认为100
            max_attempts: 最大尝试次数，默认为10
        """
        self.min_num = min_num
        self.max_num = max_num
        self.max_attempts = max_attempts
        self.target_number = None
        self.attempts_count = 0
        self.has_won = False

    def generate_target_number(self) -> int:
        """
        生成目标随机数
        
        Returns:
            生成的随机数
        """
        try:
            self.target_number = random.randint(self.min_num, self.max_num)
            return self.target_number
        except ValueError as e:
            raise ValueError(f"无效的数值范围: min={self.min_num}, max={self.max_num}") from e

    def reset_game(self):
        """
        重置游戏状态
        """
        self.target_number = None
        self.attempts_count = 0
        self.has_won = False

    def make_guess(self, guess: int) -> Tuple[bool, str]:
        """
        处理用户的一次猜测
        
        Args:
            guess: 用户猜测的数字
            
        Returns:
            Tuple[bool, str]: (是否猜对, 提示信息)
        """
        # 验证输入范围
        if not isinstance(guess, int):
            return False, f"输入必须是整数"
        
        if guess < self.min_num or guess > self.max_num:
            return False, f"输入数字必须在 {self.min_num} 到 {self.max_num} 之间"

        # 增加尝试次数
        self.attempts_count += 1

        # 判断是否超过最大尝试次数
        if self.attempts_count > self.max_attempts:
            return False, f"已达到最大尝试次数 {self.max_attempts}，游戏结束。正确答案是 {self.target_number}"

        # 如果还没有生成目标数字，则先生成
        if self.target_number is None:
            self.generate_target_number()

        # 判断猜测结果
        if guess == self.target_number:
            self.has_won = True
            return True, f"恭喜你！猜对了！答案就是 {self.target_number}，用了 {self.attempts_count} 次尝试。"
        elif guess < self.target_number:
            remaining_attempts = self.max_attempts - self.attempts_count
            return False, f"太小了！还有 {remaining_attempts} 次机会。"
        else:  # guess > self.target_number
            remaining_attempts = self.max_attempts - self.attempts_count
            return False, f"太大了！还有 {remaining_attempts} 次机会。"

    def get_game_status(self) -> dict:
        """
        获取当前游戏状态
        
        Returns:
            包含游戏状态信息的字典
        """
        return {
            "attempts_count": self.attempts_count,
            "max_attempts": self.max_attempts,
            "has_won": self.has_won,
            "target_generated": self.target_number is not None,
            "remaining_attempts": max(0, self.max_attempts - self.attempts_count)
        }

    def is_game_over(self) -> bool:
        """
        判断游戏是否结束
        
        Returns:
            游戏是否结束
        """
        return self.has_won or self.attempts_count >= self.max_attempts


def validate_range(min_val: int, max_val: int) -> bool:
    """
    验证数值范围的有效性
    
    Args:
        min_val: 最小值
        max_val: 最大值
        
    Returns:
        范围是否有效
    """
    return min_val <= max_val and min_val >= 0


def format_hint_message(is_correct: bool, message: str) -> str:
    """
    格式化提示消息
    
    Args:
        is_correct: 是否猜对
        message: 原始消息
        
    Returns:
        格式化后的消息
    """
    prefix = "[正确]" if is_correct else "[提示]"
    return f"{prefix} {message}"