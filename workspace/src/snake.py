from typing import List, Tuple
import random
from src.config import (
    GRID_WIDTH, 
    GRID_HEIGHT, 
    INITIAL_SNAKE_LENGTH,
    UP, 
    DOWN, 
    LEFT, 
    RIGHT
)

class Snake:
    """
    蛇类，管理蛇的位置、方向、移动和身体增长逻辑
    """
    
    def __init__(self):
        """
        初始化蛇对象
        - 设置初始位置（屏幕中央）
        - 设置初始长度
        - 设置初始方向为右
        """
        # 计算初始蛇头位置（屏幕中央）
        start_x = GRID_WIDTH // 2
        start_y = GRID_HEIGHT // 2
        
        # 初始化蛇的身体，从蛇头开始向左延伸
        self.body: List[Tuple[int, int]] = []
        for i in range(INITIAL_SNAKE_LENGTH):
            self.body.append((start_x - i, start_y))
        
        # 当前移动方向，默认向右
        self.direction = RIGHT
        # 下一个移动方向（用于缓冲用户输入）
        self.next_direction = RIGHT
    
    def move(self) -> bool:
        """
        移动蛇一步
        返回布尔值表示是否成功移动（未撞墙或自撞）
        """
        # 更新方向为下一个方向（如果有效）
        if self._is_valid_direction_change(self.direction, self.next_direction):
            self.direction = self.next_direction
        
        # 计算新的头部位置
        head_x, head_y = self.body[0]
        
        if self.direction == UP:
            new_head = (head_x, head_y - 1)
        elif self.direction == DOWN:
            new_head = (head_x, head_y + 1)
        elif self.direction == LEFT:
            new_head = (head_x - 1, head_y)
        elif self.direction == RIGHT:
            new_head = (head_x + 1, head_y)
        else:
            # 如果方向无效，返回失败
            return False
        
        # 检查是否撞墙
        if (new_head[0] < 0 or new_head[0] >= GRID_WIDTH or 
            new_head[1] < 0 or new_head[1] >= GRID_HEIGHT):
            return False
        
        # 检查是否撞到自己
        if new_head in self.body:
            return False
        
        # 将新头部添加到身体前端
        self.body.insert(0, new_head)
        
        # 移除尾部（除非增长了身体）
        # 在这里我们只是移动，不增长，所以移除尾部
        self.body.pop()
        
        return True
    
    def grow(self):
        """
        增长蛇的身体（通常在吃到食物时调用）
        """
        if not self.body:
            return
        
        # 获取当前尾部位置
        tail = self.body[-1]
        
        # 根据蛇的移动方向，在合适的位置添加新的尾部
        # 这里简单地在当前尾部位置添加，实际移动时会自然形成正确的形状
        self.body.append(tail)
    
    def change_direction(self, new_direction: str):
        """
        改变蛇的移动方向
        限制不能反向移动（例如不能从向上直接变为向下）
        """
        self.next_direction = new_direction
    
    def _is_valid_direction_change(self, current_dir: str, new_dir: str) -> bool:
        """
        检查方向改变是否有效
        不能反向移动（上不能变下，左不能变右等）
        """
        invalid_pairs = {
            (UP, DOWN),
            (DOWN, UP),
            (LEFT, RIGHT),
            (RIGHT, LEFT)
        }
        
        return (current_dir, new_dir) not in invalid_pairs
    
    def get_head_position(self) -> Tuple[int, int]:
        """
        获取蛇头当前位置
        """
        if not self.body:
            raise ValueError("蛇身体为空")
        return self.body[0]
    
    def get_body_positions(self) -> List[Tuple[int, int]]:
        """
        获取蛇身体所有位置
        """
        return self.body.copy()
    
    def check_collision_with_self(self) -> bool:
        """
        检查蛇是否与自身碰撞
        """
        if len(self.body) <= 1:
            return False
        
        head = self.body[0]
        # 检查头是否与身体其他部分重叠
        for segment in self.body[1:]:
            if head == segment:
                return True
        return False
    
    def reset(self):
        """
        重置蛇到初始状态
        """
        start_x = GRID_WIDTH // 2
        start_y = GRID_HEIGHT // 2
        
        self.body = []
        for i in range(INITIAL_SNAKE_LENGTH):
            self.body.append((start_x - i, start_y))
        
        self.direction = RIGHT
        self.next_direction = RIGHT