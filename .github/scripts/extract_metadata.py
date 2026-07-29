import ast
import json
import re
import sys
from pathlib import Path

def extract_plugin_metadata(file_path: str = 'main.py'):
    """从 main.py 中提取 __plugin_meta__"""
    
    if not Path(file_path).exists():
        print(f"❌ {file_path} 不存在")
        return None
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 查找 __plugin_meta__ 定义
        pattern = r'__plugin_meta__\s*=\s*({.*?})'
        match = re.search(pattern, content, re.DOTALL)
        
        if not match:
            print("❌ 未找到 __plugin_meta__")
            print("   请确保 main.py 中包含 __plugin_meta__ 定义")
            return None
        
        dict_str = match.group(1)
        
        try:
            metadata = ast.literal_eval(dict_str)
            if isinstance(metadata, dict):
                # 提取需要的字段
                filtered = {
                    'name': metadata.get('name', ''),
                    'author': metadata.get('author', ''),
                    'description': metadata.get('description', ''),
                    'version': metadata.get('version', ''),
                    'github': metadata.get('github', ''),
                }
                
                # 检查必要字段
                if not filtered['name']:
                    print("❌ 缺少 'name' 字段")
                    return None
                if not filtered['github']:
                    print("❌ 缺少 'github' 字段")
                    return None
                
                # 保存为JSON文件
                with open('metadata.json', 'w', encoding='utf-8') as f:
                    json.dump(filtered, f, ensure_ascii=False, indent=2)
                
                print(f"✅ 提取元数据成功")
                print(f"   Name: {filtered['name']}")
                print(f"   Author: {filtered['author']}")
                print(f"   Version: {filtered['version']}")
                print(f"   GitHub: {filtered['github']}")
                return filtered
            else:
                print("❌ __plugin_meta__ 不是字典格式")
                return None
        except Exception as e:
            print(f"❌ 解析 __plugin_meta__ 失败: {e}")
            return None
            
    except Exception as e:
        print(f"❌ 读取文件失败: {e}")
        return None

if __name__ == '__main__':
    result = extract_plugin_metadata()
    if result is None:
        print("::error::提取插件元数据失败，请检查 main.py 是否包含正确的 __plugin_meta__")
        sys.exit(1)
    else:
        print("::notice::插件元数据提取成功")
        sys.exit(0)
