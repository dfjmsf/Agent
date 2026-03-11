
import pygame
import sys
sys.path.insert(0, '.')

from src.display import render_game, draw_snake, draw_food, draw_score, draw_game_over
from src.game_mock import Game
from src.snake_mock import Snake, Food

def test_display_functions():
    pygame.init()
    screen = pygame.display.set_mode((800, 600))
    font = pygame.font.Font(None, 36)
    
    # ≤‚ ‘ draw_snake
    snake = Snake()
    draw_snake(screen, snake)
    
    # ≤‚ ‘ draw_food
    food = Food()
    draw_food(screen, food)
    
    # ≤‚ ‘ draw_score
    draw_score(screen, 100, font)
    
    # ≤‚ ‘ draw_game_over
    draw_game_over(screen, font)
    
    # ≤‚ ‘ render_game
    game = Game()
    render_game(game)
    
    pygame.quit()
    print("À˘”–œ‘ æ∫Ø ˝≤‚ ‘Õ®π˝£°")

if __name__ == "__main__":
    test_display_functions()
