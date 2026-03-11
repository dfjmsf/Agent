"""
泰拉瑞亚合成表数据提取器

该模块提供将解析后的泰拉瑞亚合成表数据转换为结构化格式的功能，
将原始解析结果整理为包含物品名、材料列表、合成站的字典列表。
"""

from typing import List, Dict, Any, Optional


def extract_recipes_data(parsed_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将解析后的合成表数据提取为标准化的字典列表格式
    
    Args:
        parsed_data: 解析器返回的原始数据列表，每个元素包含物品信息
        
    Returns:
        List[Dict[str, Any]]: 标准化的合成配方列表，每个配方包含物品名、材料列表、合成站
    """
    extracted_recipes = []
    
    for recipe in parsed_data:
        try:
            # 提取物品名称，优先使用完整名称，如果没有则使用基础名称
            item_name = recipe.get('item_name', '').strip()
            
            # 提取材料列表，确保材料列表存在且为有效格式
            materials = recipe.get('materials', [])
            if not isinstance(materials, list):
                materials = []
            
            # 清理材料数据，移除空值
            cleaned_materials = []
            for material in materials:
                if isinstance(material, dict):
                    mat_name = material.get('name', '').strip()
                    mat_count = material.get('count', 1)
                    if mat_name:
                        cleaned_materials.append({
                            'name': mat_name,
                            'count': int(mat_count) if mat_count else 1
                        })
                elif isinstance(material, str) and material.strip():
                    cleaned_materials.append({
                        'name': material.strip(),
                        'count': 1
                    })
            
            # 提取合成站信息
            crafting_station = recipe.get('crafting_station', '').strip()
            
            # 构建标准化的配方字典
            standardized_recipe = {
                'item_name': item_name,
                'materials': cleaned_materials,
                'crafting_station': crafting_station
            }
            
            # 只有当物品名称存在时才添加到结果中
            if item_name:
                extracted_recipes.append(standardized_recipe)
                
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            # 遇到错误时跳过当前配方，继续处理下一个
            continue
    
    return extracted_recipes


def filter_recipes_by_item_name(extracted_recipes: List[Dict[str, Any]], item_name: str) -> List[Dict[str, Any]]:
    """
    根据物品名称过滤合成配方
    
    Args:
        extracted_recipes: 已提取的合成配方列表
        item_name: 要查找的物品名称
        
    Returns:
        List[Dict[str, Any]]: 匹配的合成配方列表
    """
    filtered = []
    target_name_lower = item_name.lower().strip()
    
    for recipe in extracted_recipes:
        if recipe.get('item_name', '').lower() == target_name_lower:
            filtered.append(recipe)
    
    return filtered


def get_all_materials_for_item(extracted_recipes: List[Dict[str, Any]], item_name: str) -> List[Dict[str, Any]]:
    """
    获取制作指定物品所需的全部材料
    
    Args:
        extracted_recipes: 已提取的合成配方列表
        item_name: 目标物品名称
        
    Returns:
        List[Dict[str, Any]]: 所需材料列表
    """
    recipes = filter_recipes_by_item_name(extracted_recipes, item_name)
    if not recipes:
        return []
    
    # 返回第一个匹配配方的材料列表
    return recipes[0].get('materials', [])


def get_crafting_station_for_item(extracted_recipes: List[Dict[str, Any]], item_name: str) -> Optional[str]:
    """
    获取制作指定物品所需的合成站
    
    Args:
        extracted_recipes: 已提取的合成配方列表
        item_name: 目标物品名称
        
    Returns:
        Optional[str]: 合成站名称，如果不存在则返回None
    """
    recipes = filter_recipes_by_item_name(extracted_recipes, item_name)
    if not recipes:
        return None
    
    return recipes[0].get('crafting_station')