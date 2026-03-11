
from src.snake_mock import Snake, Food
import pygame

class Game:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((800, 600))
        self.font = pygame.font.Font(None, 36)
        self.snake = Snake()
        self.food = Food()
        self.score = 0
        self.game_over = False
