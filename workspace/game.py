import random

class NumberGuessingGame:
    """
    数字猜谜游戏类
    实现生成随机数、接收用户输入、比较并给出提示、统计猜测次数等功能
    """
    
    def __init__(self, min_num=1, max_num=100):
        """
        初始化游戏参数
        
        Args:
            min_num (int): 随机数最小值，默认为1
            max_num (int): 随机数最大值，默认为100
        """
        self.min_num = min_num
        self.max_num = max_num
        self.target_number = None
        self.guess_count = 0
        self.game_over = False
    
    def start_new_game(self):
        """
        开始新游戏，生成新的目标数字并重置计数器
        """
        try:
            self.target_number = random.randint(self.min_num, self.max_num)
            self.guess_count = 0
            self.game_over = False
        except Exception as e:
            raise ValueError(f"无法生成随机数: {e}")
    
    def make_guess(self, user_guess):
        """
        处理用户的猜测
        
        Args:
            user_guess (int): 用户输入的猜测数字
            
        Returns:
            str: 提示信息（"太大了", "太小了", "恭喜猜对了"）
            
        Raises:
            ValueError: 当输入超出范围或不是有效数字时抛出
        """
        if self.game_over:
            return "游戏已结束，请重新开始"
        
        if not isinstance(user_guess, int):
            raise ValueError("输入必须是整数")
        
        if user_guess < self.min_num or user_guess > self.max_num:
            raise ValueError(f"输入必须在 {self.min_num} 到 {self.max_num} 之间")
        
        self.guess_count += 1
        
        if user_guess > self.target_number:
            return "太大了"
        elif user_guess < self.target_number:
            return "太小了"
        else:
            self.game_over = True
            return f"恭喜猜对了！答案是 {self.target_number}，总共猜了 {self.guess_count} 次"
    
    def get_guess_count(self):
        """
        获取当前猜测次数
        
        Returns:
            int: 当前猜测次数
        """
        return self.guess_count
    
    def is_game_over(self):
        """
        检查游戏是否结束
        
        Returns:
            bool: 游戏是否结束
        """
        return self.game_over


def main():
    """
    主函数，用于交互式游戏
    """
    try:
        game = NumberGuessingGame()
        game.start_new_game()
        
        print(f"欢迎来到数字猜谜游戏！请猜一个 {game.min_num} 到 {game.max_num} 之间的数字。")
        
        while not game.is_game_over():
            try:
                user_input = input("请输入你的猜测: ")
                
                # 尝试转换输入为整数
                try:
                    guess = int(user_input)
                except ValueError:
                    print("请输入一个有效的整数！")
                    continue
                
                result = game.make_guess(guess)
                print(result)
                
            except ValueError as e:
                print(f"输入错误: {e}")
            except KeyboardInterrupt:
                print("\n游戏被中断")
                break
            except Exception as e:
                print(f"发生未知错误: {e}")
                break
        
    except Exception as e:
        print(f"游戏初始化失败: {e}")


if __name__ == "__main__":
    main()