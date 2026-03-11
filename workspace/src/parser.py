"""
泰拉瑞亚合成表解析器

该模块提供解析泰拉瑞亚合成表的功能，能够从指定路径读取合成表文件，
识别物品名称、所需材料及合成站类型，并忽略所有备注内容。
"""

import re
from typing import List, Dict, Optional


class TerrariaRecipeParser:
    """
    泰拉瑞亚合成表解析器类
    
    提供解析泰拉瑞亚游戏合成表的功能，支持识别物品名称、材料需求和合成站。
    """
    
    def __init__(self):
        """初始化解析器"""
        self.recipes = []
    
    def _is_comment_line(self, line: str) -> bool:
        """
        判断是否为注释行
        
        Args:
            line: 待判断的行字符串
            
        Returns:
            bool: 如果是注释行返回True，否则返回False
        """
        stripped_line = line.strip()
        return stripped_line.startswith('//')
    
    def _is_empty_line(self, line: str) -> bool:
        """
        判断是否为空行
        
        Args:
            line: 待判断的行字符串
            
        Returns:
            bool: 如果是空行返回True，否则返回False
        """
        return not line.strip()
    
    def _extract_recipe_info(self, line: str) -> Optional[Dict[str, str]]:
        """
        从行中提取配方信息
        
        Args:
            line: 包含配方信息的行
            
        Returns:
            dict: 包含物品名称、材料和合成站的字典，如果无法解析则返回None
        """
        # 使用正则表达式匹配格式：物品名称 <- 材料 -> 合成站
        pattern = r'^\s*([^<-]+?)\s*<-\s*(.+?)\s*->\s*(.+?)\s*$'
        match = re.match(pattern, line.strip())
        
        if match:
            item_name = match.group(1).strip()
            materials_str = match.group(2).strip()
            crafting_station = match.group(3).strip()
            
            # 解析材料，按逗号分割
            materials = [material.strip() for material in materials_str.split(',')]
            
            return {
                'item': item_name,
                'materials': materials,
                'station': crafting_station
            }
        
        return None
    
    def parse_file(self, file_path: str) -> List[Dict[str, any]]:
        """
        解析泰拉瑞亚合成表文件
        
        Args:
            file_path: 文件路径
            
        Returns:
            list: 包含所有配方信息的列表
        """
        recipes = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                lines = file.readlines()
                
            i = 0
            while i < len(lines):
                line = lines[i]
                
                # 跳过注释行和空行
                if self._is_comment_line(line) or self._is_empty_line(line):
                    i += 1
                    continue
                
                # 尝试解析配方信息
                recipe_info = self._extract_recipe_info(line)
                if recipe_info:
                    recipes.append(recipe_info)
                
                i += 1
                
        except FileNotFoundError:
            print(f"错误：找不到文件 {file_path}")
        except Exception as e:
            print(f"解析文件时发生错误: {e}")
        
        return recipes


def parse_terraria_recipes(file_path: str) -> List[Dict[str, any]]:
    """
    解析泰拉瑞亚合成表的便捷函数
    
    Args:
        file_path: 泰拉瑞亚合成表文件路径
        
    Returns:
        list: 包含所有配方信息的列表
    """
    parser = TerrariaRecipeParser()
    return parser.parse_file(file_path)


if __name__ == "__main__":
    # 示例用法
    import sys
    
    if len(sys.argv) != 2:
        print("用法: python parser.py <文件路径>")
        sys.exit(1)
    
    file_path = sys.argv[1]
    recipes = parse_terraria_recipes(file_path)
    
    for idx, recipe in enumerate(recipes, 1):
        print(f"配方 {idx}:")
        print(f"  物品: {recipe['item']}")
        print(f"  材料: {', '.join(recipe['materials'])}")
        print(f"  合成站: {recipe['station']}")
        print()