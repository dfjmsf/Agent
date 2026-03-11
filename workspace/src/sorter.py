def rank(distances_labels):
    """
    对 [距离, 标签] 列表按距离升序排序
    
    Args:
        distances_labels: 距离和标签的列表，格式为 [[距离1, 标签1], [距离2, 标签2], ...]
        
    Returns:
        list: 按距离升序排序后的 [距离, 标签] 列表
        
    Raises:
        TypeError: 当输入参数不是列表，或者列表元素不是包含两个元素的列表或元组时抛出
        ValueError: 当距离不是数字时抛出
    """
    try:
        # 验证输入参数类型
        if not isinstance(distances_labels, list):
            raise TypeError("输入参数必须是列表")
        
        # 验证列表中每个元素的格式
        for item in distances_labels:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise TypeError("列表中的每个元素必须是包含两个元素的列表或元组 [距离, 标签]")
            
            distance, label = item
            
            # 验证距离是否为数字
            if not isinstance(distance, (int, float)):
                raise ValueError(f"距离必须是数字，得到的是 {type(distance)} 类型: {distance}")
        
        # 按距离升序排序
        sorted_list = sorted(distances_labels, key=lambda x: x[0])
        
        return sorted_list
        
    except Exception as e:
        # 捕获所有可能的异常并重新抛出
        raise e


if __name__ == "__main__":
    # 示例用法
    distances_and_labels = [
        [3.5, 'A'],
        [1.2, 'B'],
        [2.8, 'C'],
        [0.9, 'D'],
        [4.1, 'E']
    ]
    
    result = rank(distances_and_labels)
    print(result)