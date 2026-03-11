import sys
from guess_number import GuessNumberGame
from utils import validate_input


def main():
    """
    游戏主入口函数
    处理游戏流程控制和用户交互
    """
    print("欢迎来到猜数字游戏！")
    
    while True:
        try:
            # 创建游戏实例
            game = GuessNumberGame()
            
            print(f"我已经想好了一个 {game.min_num} 到 {game.max_num} 之间的数字，请开始猜测吧！")
            
            # 游戏主循环
            while True:
                try:
                    user_input = input("请输入您的猜测: ")
                    
                    # 验证输入是否为有效数字
                    is_valid, processed_input = validate_input(user_input, "number")
                    if not is_valid:
                        print("无效输入，请输入一个数字")
                        continue
                    
                    # 获取游戏反馈
                    result = game.make_guess(processed_input)
                    print(result)
                    
                    # 检查是否猜对
                    if "恭喜" in result:
                        break
                        
                except KeyboardInterrupt:
                    print("\n游戏被中断")
                    sys.exit(0)
                except Exception as e:
                    print(f"发生错误: {e}")
                    continue
            
            # 游戏结束后询问是否继续
            while True:
                try:
                    play_again = input("是否要再玩一次？(y/n): ").strip().lower()
                    if play_again in ['y', 'yes', '是']:
                        break  # 继续外层循环开始新游戏
                    elif play_again in ['n', 'no', '否']:
                        print("谢谢游玩，再见！")
                        return  # 结束程序
                    else:
                        print("请输入 y 或 n")
                except KeyboardInterrupt:
                    print("\n游戏被中断")
                    sys.exit(0)
                    
        except KeyboardInterrupt:
            print("\n游戏被中断")
            sys.exit(0)
        except Exception as e:
            print(f"游戏过程中发生错误: {e}")
            continue


if __name__ == "__main__":
    main()