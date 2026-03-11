from typing import List, Tuple


def _gen() -> Tuple[List[Tuple[float, float]], List[int]]:
    """
    生成固定的训练数据点及其对应的二分类标签
    
    Returns:
        tuple: 包含两个元素的元组
               - 第一个元素：数据点列表，每个数据点为(x, y)坐标元组
               - 第二个元素：对应的标签列表，值为0或1
    """
    # 定义固定的数据点坐标
    data_points = [
        (1.0, 2.0),
        (2.0, 3.0),
        (3.0, 1.0),
        (4.0, 5.0),
        (5.0, 4.0),
        (6.0, 2.0),
        (7.0, 6.0),
        (8.0, 3.0),
        (9.0, 7.0),
        (10.0, 1.0)
    ]
    
    # 定义对应的二分类标签
    labels = [0, 1, 0, 1, 1, 0, 1, 0, 1, 0]
    
    return data_points, labels