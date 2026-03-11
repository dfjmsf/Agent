import random


class GuessNumberGame:
    def __init__(self, min_num=1, max_num=100):
        """
        初始化猜数字游戏
        :param min_num: 最小数字范围
        :param max_num: 最大数字范围
        """
        self.min_num = min_num
        self.max_num = max_num
        # 生成目标数字
        self.target_number = random.randint(min_num, max_num)
        self.attempts = 0  # 记录尝试次数

    def make_guess(self, guess):
        """
        处理用户的猜测
        :param guess: 用户猜测的数字
        :return: 提示信息字符串
        """
        try:
            # 将输入转换为整数
            guess = int(guess)
        except (ValueError, TypeError):
            return "请输入一个有效的数字"

        # 增加尝试次数
        self.attempts += 1

        # 判断猜测结果
        if guess < self.target_number:
            return "太小了，请再试试更大的数字"
        elif guess > self.target_number:
            return "太大了，请再试试更小的数字"
        else:
            return f"恭喜你！猜对了！答案就是 {self.target_number}，你用了 {self.attempts} 次尝试"

    def reset_game(self):
        """
        重置游戏，生成新的目标数字
        """
        self.target_number = random.randint(self.min_num, self.max_num)
        self.attempts = 0

    def get_target_number(self):
        """
        获取当前目标数字（用于测试）
        :return: 目标数字
        """
        return self.target_number


def main():
    """
    主函数，运行猜数字游戏
    """
    print("欢迎来到猜数字游戏！")
    
    # 获取用户设定的数字范围
    try:
        min_num = int(input("请输入最小数字（默认1）：") or 1)
        max_num = int(input("请输入最大数字（默认100）：") or 100)
        
        if min_num >= max_num:
            print("最小数字必须小于最大数字，使用默认范围 1-100")
            min_num, max_num = 1, 100
            
    except ValueError:
        print("输入无效，使用默认范围 1-100")
        min_num, max_num = 1, 100

    # 创建游戏实例
    game = GuessNumberGame(min_num, max_num)
    print(f"我已经想好了一个 {min_num} 到 {max_num} 之间的数字，请开始猜测吧！")

    while True:
        try:
            user_input = input("请输入你的猜测（输入 'quit' 退出游戏）：")
            
            if user_input.lower() == 'quit':
                print(f"游戏结束。正确答案是 {game.get_target_number()}")
                break

            result = game.make_guess(user_input)
            print(result)

            # 如果猜对了，询问是否继续游戏
            if f"恭喜你！猜对了！答案就是 {game.get_target_number()}" in result:
                play_again = input("是否再玩一次？(y/n): ")
                if play_again.lower() == 'y':
                    game.reset_game()
                    print(f"新游戏开始！我想好了 {min_num} 到 {max_num} 之间的一个新数字。")
                else:
                    print("谢谢游玩，再见！")
                    break

        except KeyboardInterrupt:
            print("\n游戏被中断，再见！")
            break
        except Exception as e:
            print(f"发生错误：{e}")


if __name__ == "__main__":
    main()