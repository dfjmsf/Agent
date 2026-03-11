import sys
from src.game import NumberGuessingGame


def main():
    """
    游戏主入口函数
    处理命令行参数并启动猜数字游戏
    """
    try:
        # 解析命令行参数
        if len(sys.argv) >= 3:
            min_num = int(sys.argv[1])
            max_num = int(sys.argv[2])
        elif len(sys.argv) == 2:
            min_num = 1
            max_num = int(sys.argv[1])
        else:
            min_num = 1
            max_num = 100

        # 创建游戏实例
        game = NumberGuessingGame(min_num=min_num, max_num=max_num)
        
        # 开始新游戏
        game.start_new_game()
        
        print(f"欢迎来到猜数字游戏！")
        print(f"我已经想好了一个 {min_num} 到 {max_num} 之间的数字，请开始猜测吧！")

        # 游戏主循环
        while True:
            try:
                user_input = input("请输入您的猜测: ")
                
                # 尝试转换输入为整数
                try:
                    guess = int(user_input)
                except ValueError:
                    print("请输入一个有效的整数！")
                    continue
                
                # 验证输入范围
                if guess < min_num or guess > max_num:
                    print(f"请输入 {min_num} 到 {max_num} 之间的数字！")
                    continue
                
                # 处理猜测
                result = game.make_guess(guess)
                
                if result['status'] == 'correct':
                    print(f"恭喜您！猜对了！答案就是 {result['target_number']}，您总共用了 {result['attempts']} 次尝试。")
                    break
                elif result['status'] == 'too_low':
                    print("太小了，请再试试更大的数字！")
                elif result['status'] == 'too_high':
                    print("太大了，请再试试更小的数字！")
                    
            except KeyboardInterrupt:
                print("\n游戏被中断，再见！")
                break
            except EOFError:
                print("\n输入结束，游戏退出。")
                break
            except Exception as e:
                print(f"发生未知错误: {e}")
                break

    except ValueError as ve:
        print(f"参数错误: 请提供有效的数字参数。{ve}")
    except Exception as e:
        print(f"程序启动失败: {e}")


if __name__ == "__main__":
    main()