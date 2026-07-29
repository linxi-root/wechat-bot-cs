import ast
import re
import sys
from pathlib import Path

def extract_metadata(file_path):
    """从 Python 文件中提取 __plugin_meta__ 字典"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        pattern = r'__plugin_meta__\s*=\s*({.*?})'
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            return None
        
        dict_str = match.group(1)
        try:
            metadata = ast.literal_eval(dict_str)
            if isinstance(metadata, dict):
                # 只比较元数据相关的字段
                return {
                    'name': metadata.get('name', ''),
                    'author': metadata.get('author', ''),
                    'description': metadata.get('description', ''),
                    'version': metadata.get('version', ''),
                    'github': metadata.get('github', ''),
                }
        except:
            pass
        return None
    except:
        return None

def compare_metadata(old_file, new_file):
    """比较两个文件的元数据是否一致"""
    old_meta = extract_metadata(old_file)
    new_meta = extract_metadata(new_file)
    
    # 如果旧文件没有元数据，视为发生变化
    if old_meta is None and new_meta is not None:
        print("✅ 检测到新的元数据")
        with open('metadata_changed.txt', 'w') as f:
            f.write('has_changes=true')
        return True
    
    # 如果新文件没有元数据，视为无变化
    if new_meta is None:
        print("ℹ️  main.py 中未找到 __plugin_meta__")
        return False
    
    # 比较字典是否一致
    if old_meta == new_meta:
        print("ℹ️  元数据未发生变化")
        return False
    
    # 输出变化详情
    print("✅ 检测到元数据变化:")
    for key in old_meta.keys():
        if old_meta.get(key) != new_meta.get(key):
            print(f"   {key}: '{old_meta.get(key)}' -> '{new_meta.get(key)}'")
    
    with open('metadata_changed.txt', 'w') as f:
        f.write('has_changes=true\n')
        for key, value in new_meta.items():
            f.write(f'{key}: {value}\n')
    
    return True

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法: python extract_metadata_compare.py <old_file> <new_file>")
        sys.exit(1)
    
    old_file = sys.argv[1]
    new_file = sys.argv[2]
    
    if not Path(old_file).exists():
        # 如果是首次提交，旧文件不存在
        old_file = '/dev/null'
    
    changed = compare_metadata(old_file, new_file)
    sys.exit(0 if changed else 1)
