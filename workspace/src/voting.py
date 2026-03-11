from typing import List, Tuple, Union


def vote(distances_labels: List[Union[List, Tuple]], k: int) -> int:
    """
    实现投票函数，接收排序后的前k个 [距离, 标签] 数据，返回多数类别的预测结果
    
    Args:
        distances_labels: 排序后的[距离, 标签]列表，格式为[[距离1, 标签1], [距离2, 标签2], ...]
        k: 选取前k个最近邻样本进行投票
        
    Returns:
        int: 投票结果，即出现次数最多的标签
        
    Raises:
        TypeError: 当输入参数类型不正确时抛出
        ValueError: 当k值不合理或distances_labels为空时抛出
    """
    try:
        # 验证输入参数类型
        if not isinstance(distances_labels, list):
            raise TypeError("distances_labels必须是列表")
        
        if not isinstance(k, int):
            raise TypeError("k必须是整数")
        
        # 验证k值合理性
        if k <= 0:
            raise ValueError("k必须大于0")
        
        if k > len(distances_labels):
            raise ValueError("k不能大于distances_labels的长度")
        
        if len(distances_labels) == 0:
            raise ValueError("distances_labels不能为空")
        
        # 验证列表中每个元素的格式
        for item in distances_labels:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise TypeError("列表中的每个元素必须是包含两个元素的列表或元组 [距离, 标签]")
        
        # 获取前k个样本
        top_k = distances_labels[:k]
        
        # 统计各标签出现次数
        label_count = {}
        for _, label in top_k:
            if not isinstance(label, int):
                raise TypeError("标签必须是整数")
            label_count[label] = label_count.get(label, 0) + 1
        
        # 找到出现次数最多的标签
        max_count = 0
        result_label = None
        for label, count in label_count.items():
            if count > max_count:
                max_count = count
                result_label = label
            elif count == max_count and (result_label is None or label < result_label):
                # 如果票数相同，选择较小的标签值（保证确定性）
                result_label = label
        
        return result_label
    
    except Exception as e:
        # 捕获可能的其他异常
        raise e


if __name__ == "__main__":
    # 示例用法
    distances_labels = [
        [1.0, 0],
        [2.0, 1],
        [3.0, 0],
        [4.0, 1],
        [5.0, 0]
    ]
    k = 3
    result = vote(distances_labels, k)
    print(result)