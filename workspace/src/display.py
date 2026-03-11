import sys
from typing import List, Tuple
from src.config import (
    WINDOW_WIDTH, 
    WINDOW_HEIGHT, 
    GRID_SIZE,
    BLACK, 
    WHITE, 
    RED, 
    GREEN, 
    YELLOW, 
    CYAN,
    SNAKE_COLOR, 
    SNAKE_HEAD_COLOR, 
    FOOD_COLOR,
    SCORE_COLOR,
    GAME_OVER_COLOR
)

class Display:
    """
    显示类，负责渲染游戏画面
    包括蛇、食物、得分和游戏结束界面的绘制
    """
    
    def __init__(self):
        """
        初始化显示对象
        """
        self.width = WINDOW_WIDTH
        self.height = WINDOW_HEIGHT
        self.grid_size = GRID_SIZE
        self.screen_buffer = None  # 模拟屏幕缓冲区
    
    def draw_snake(self, snake_body: List[Tuple[int, int]], screen=None):
        """
        绘制蛇
        
        Args:
            snake_body: 蛇的身体坐标列表 [(x, y), ...]
            screen: 屏幕对象（模拟参数）
        """
        if not snake_body:
            return
            
        # 绘制蛇身（除头部外）
        for i, (x, y) in enumerate(snake_body):
            if i == 0:  # 蛇头
                self._draw_grid_cell(x, y, SNAKE_HEAD_COLOR, screen)
            else:  # 蛇身
                self._draw_grid_cell(x, y, SNAKE_COLOR, screen)
    
    def draw_food(self, food_position: Tuple[int, int], screen=None):
        """
        绘制食物
        
        Args:
            food_position: 食物坐标 (x, y)
            screen: 屏幕对象（模拟参数）
        """
        x, y = food_position
        self._draw_grid_cell(x, y, FOOD_COLOR, screen)
    
    def draw_score(self, score: int, screen=None):
        """
        绘制得分
        
        Args:
            score: 当前得分
            screen: 屏幕对象（模拟参数）
        """
        # 这里只是模拟绘制逻辑，实际在真实pygame中会绘制文字
        print(f"Score: {score}")
    
    def draw_game_over(self, screen=None):
        """
        绘制游戏结束界面
        
        Args:
            screen: 屏幕对象（模拟参数）
        """
        # 模拟绘制游戏结束提示
        print("Game Over!")
        print("Press R to restart or Q to quit")
    
    def _draw_grid_cell(self, grid_x: int, grid_y: int, color: Tuple[int, int, int], screen=None):
        """
        绘制单个网格单元
        
        Args:
            grid_x: 网格X坐标
            grid_y: 网格Y坐标
            color: 颜色 (R, G, B)
            screen: 屏幕对象（模拟参数）
        """
        # 将网格坐标转换为像素坐标
        pixel_x = grid_x * self.grid_size
        pixel_y = grid_y * self.grid_size
        
        # 模拟绘制矩形的操作
        # 在实际pygame中会调用 pygame.draw.rect(screen, color, (pixel_x, pixel_y, self.grid_size, self.grid_size))
        pass
    
    def clear_screen(self, screen=None):
        """
        清空屏幕
        
        Args:
            screen: 屏幕对象（模拟参数）
        """
        # 模拟清屏操作
        # 在实际pygame中会调用 screen.fill(BLACK)
        pass
    
    def update_display(self, screen=None):
        """
        更新显示
        
        Args:
            screen: 屏幕对象（模拟参数）
        """
        # 模拟更新显示操作
        # 在实际pygame中会调用 pygame.display.flip() 或 pygame.display.update()
        pass
    
    def render_frame(self, snake_body: List[Tuple[int, int]], 
                    food_position: Tuple[int, int], 
                    score: int, 
                    game_over: bool, 
                    screen=None):
        """
        渲染整个游戏帧
        
        Args:
            snake_body: 蛇的身体坐标列表
            food_position: 食物坐标
            score: 当前得分
            game_over: 游戏是否结束
            screen: 屏幕对象（模拟参数）
        """
        # 清空屏幕
        self.clear_screen(screen)
        
        # 绘制游戏元素
        self.draw_snake(snake_body, screen)
        self.draw_food(food_position, screen)
        self.draw_score(score, screen)
        
        # 如果游戏结束，绘制结束界面
        if game_over:
            self.draw_game_over(screen)
        
        # 更新显示
        self.update_display(screen)