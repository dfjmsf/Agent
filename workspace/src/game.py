import random

class NumberGuessingGame:
    """猜数字游戏主逻辑类"""
    
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
        self.attempts = 0
    
    def start_new_game(self):
        """开始新游戏，生成目标数字并重置尝试次数"""
        try:
            self.target_number = random.randint(self.min_num, self.max_num)
            self.attempts = 0
        except Exception as e:
            raise RuntimeError(f"游戏初始化失败: {e}")
    
    def make_guess(self, guess):
        """
        处理用户猜测
        
        Args:
            guess (int): 用户猜测的数字
            
        Returns:
            str: 游戏反馈信息
        """
        try:
            # 验证输入范围
            if not isinstance(guess, int) or guess < self.min_num or guess > self.max_num:
                return f"请输入{self.min_num}到{self.max_num}之间的整数"
            
            self.attempts += 1
            
            if guess == self.target_number:
                return f"恭喜你！猜对了！答案就是{self.target_number}，总共用了{self.attempts}次尝试。"
            elif guess < self.target_number:
                return "太小了，请再试一次"
            else:
                return "太大了，请再试一次"
                
        except TypeError:
            return "请输入一个有效的整数"
        except Exception as e:
            return f"处理猜测时发生错误: {e}"
    
    def get_target_number(self):
        """获取当前目标数字（用于调试）"""
        return self.target_number
    
    def get_attempts_count(self):
        """获取当前尝试次数"""
        return self.attempts


def main():
    """主函数，处理用户交互"""
    try:
        game = NumberGuessingGame()
        game.start_new_game()
        
        print(f"欢迎来到猜数字游戏！我已经想好了一个{game.min_num}到{game.max_num}之间的数字。")
        
        while True:
            try:
                user_input = input("请输入你的猜测: ")
                guess = int(user_input)
                result = game.make_guess(guess)
                print(result)
                
                # 如果猜对了，退出循环
                if f"猜对了" in result:
                    break
                    
            except ValueError:
                print("请输入一个有效的整数")
            except KeyboardInterrupt:
                print("\n游戏被中断")
                break
            except EOFError:
                print("\n输入结束")
                break
                
    except Exception as e:
        print(f"游戏运行出错: {e}")


if __name__ == "__main__":
    main()