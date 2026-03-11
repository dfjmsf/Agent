from typing import Tuple
import random
from src.config import GRID_WIDTH, GRID_HEIGHT

class Food:
    """
    食物类，管理食物的位置和重生逻辑
    """
    
    def __init__(self):
        """
        初始化食物对象
        - 设置初始位置为随机位置
        """
        self.position = self._generate_random_position()
    
    def _get_all_possible_positions(self) -> list:
        """
        获取所有可能的网格位置
        
        Returns:
            list: 所有可能的网格坐标列表
        """
        all_positions = []
        for x in range(GRID_WIDTH):
            for y in range(GRID_HEIGHT):
                all_positions.append((x, y))
        return all_positions
    
    def _generate_random_position(self) -> Tuple[int, int]:
        """
        生成随机位置
        
        Returns:
            Tuple[int, int]: 随机生成的网格坐标 (x, y)
        """
        x = random.randint(0, GRID_WIDTH - 1)
        y = random.randint(0, GRID_HEIGHT - 1)
        return (x, y)
    
    def respawn(self, snake_body: list) -> None:
        """
        在有效区域内重新生成食物位置，确保不与蛇身重叠
        
        Args:
            snake_body (list): 蛇身的所有坐标列表
        """
        # 将蛇身坐标转换为集合以提高查找效率
        snake_body_set = set(snake_body)
        
        # 获取所有可能的位置
        all_positions = self._get_all_possible_positions()
        
        # 过滤掉蛇身占用的位置
        available_positions = [
            pos for pos in all_positions 
            if pos not in snake_body_set
        ]
        
        # 如果存在可用位置，则从中随机选择一个
        if available_positions:
            self.position = random.choice(available_positions)
        else:
            # 理论上不应该发生，因为如果蛇身占满了整个区域，游戏应该已经结束
            # 但为了代码健壮性，仍然处理这种情况
            self.position = self._generate_random_position()
    
    def get_position(self) -> Tuple[int, int]:
        """
        获取当前食物位置
        
        Returns:
            Tuple[int, int]: 当前食物的网格坐标 (x, y)
        """
        return self.position
# EOF