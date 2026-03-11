import math

def calc_dist(point1, point2):
    """
    计算两个二维点之间的欧几里得距离
    
    Args:
        point1: 第一个点的坐标，格式为 (x1, y1)
        point2: 第二个点的坐标，格式为 (x2, y2)
        
    Returns:
        float: 两点之间的欧几里得距离
        
    Raises:
        TypeError: 当输入参数不是元组或列表，或者元素不是数字时抛出
        ValueError: 当输入的点不是二维坐标时抛出
    """
    try:
        # 验证输入参数类型
        if not isinstance(point1, (tuple, list)) or not isinstance(point2, (tuple, list)):
            raise TypeError("点坐标必须是元组或列表")
        
        # 验证输入参数长度
        if len(point1) != 2 or len(point2) != 2:
            raise ValueError("点坐标必须是二维坐标 (x, y)")
        
        # 验证坐标元素是否为数字
        x1, y1 = point1
        x2, y2 = point2
        
        if not all(isinstance(coord, (int, float)) for coord in [x1, y1, x2, y2]):
            raise TypeError("坐标值必须是数字")
        
        # 计算欧几里得距离
        dist = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        return dist
        
    except TypeError as e:
        raise e
    except ValueError as e:
        raise e
    except Exception as e:
        raise ValueError(f"计算距离时发生未知错误: {str(e)}")

if __name__ == "__main__":
    # 示例用法
    p1 = (0, 0)
    p2 = (3, 4)
    distance = calc_dist(p1, p2)
    print(f"点{p1}到点{p2}的距离是: {distance}")