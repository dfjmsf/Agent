import math
from collections import Counter

def euclidean_distance(point1, point2):
    """
    计算两个点之间的欧几里得距离
    :param point1: 第一个点的坐标 (list or tuple)
    :param point2: 第二个点的坐标 (list or tuple)
    :return: 欧几里得距离 (float)
    """
    if len(point1) != len(point2):
        raise ValueError("两个点的维度必须相同")
    
    squared_diffs = [(a - b) ** 2 for a, b in zip(point1, point2)]
    return math.sqrt(sum(squared_diffs))

def get_k_nearest_neighbors(training_data, test_point, k):
    """
    获取k个最近邻
    :param training_data: 训练数据集，格式为 [(特征向量, 标签), ...]
    :param test_point: 测试点的特征向量
    :param k: 邻居数量
    :return: k个最近邻的标签列表
    """
    if k <= 0:
        raise ValueError("k必须大于0")
    if len(training_data) < k:
        raise ValueError(f"训练数据数量({len(training_data)})少于k值({k})")
    
    # 计算测试点到所有训练点的距离
    distances = []
    for features, label in training_data:
        dist = euclidean_distance(test_point, features)
        distances.append((dist, label))
    
    # 按距离排序并取前k个
    distances.sort(key=lambda x: x[0])
    k_nearest = distances[:k]
    
    # 返回对应的标签
    return [label for _, label in k_nearest]

def classify_by_knn(training_data, test_point, k):
    """
    使用KNN算法对测试点进行分类
    :param training_data: 训练数据集，格式为 [(特征向量, 标签), ...]
    :param test_point: 测试点的特征向量
    :param k: 邻居数量
    :return: 分类结果（标签）
    """
    if not training_data:
        raise ValueError("训练数据不能为空")
    
    # 获取k个最近邻的标签
    k_nearest_labels = get_k_nearest_neighbors(training_data, test_point, k)
    
    # 统计各标签出现次数
    label_counts = Counter(k_nearest_labels)
    
    # 返回出现次数最多的标签
    most_common_label = label_counts.most_common(1)[0][0]
    return most_common_label

def main():
    """
    主函数：演示KNN分类器的使用
    """
    # 示例训练数据：[(特征1, 特征2), 标签]
    training_data = [
        ([2, 3], 'A'),
        ([5, 4], 'B'),
        ([3, 8], 'C'),
        ([7, 2], 'B'),
        ([8, 5], 'B'),
        ([1, 1], 'A'),
        ([9, 6], 'B'),
        ([4, 7], 'C')
    ]
    
    # 测试点
    test_point = [5, 5]
    
    # 设置k值
    k = 3
    
    try:
        # 获取k个最近邻的标签
        k_nearest_labels = get_k_nearest_neighbors(training_data, test_point, k)
        
        # 根据前k个最近邻进行投票并输出分类结果
        prediction = classify_by_knn(training_data, test_point, k)
        
        print(f"测试点: {test_point}")
        print(f"k值: {k}")
        print(f"k个最近邻的标签: {k_nearest_labels}")
        print(f"预测结果: {prediction}")
        
        # 显示投票详情
        label_counts = Counter(k_nearest_labels)
        print("投票详情:")
        for label, count in label_counts.items():
            print(f"  标签 '{label}': {count} 票")
            
    except Exception as e:
        print(f"发生错误: {e}")

if __name__ == "__main__":
    main()