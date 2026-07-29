import json
import sys
from pathlib import Path

def update_plugins_json(metadata_path: str, target_plugins_json: str = 'plugins.json'):
    """更新目标仓库的 plugins.json"""
    
    # 读取提取的元数据
    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            new_metadata = json.load(f)
    except FileNotFoundError:
        print(f"❌ {metadata_path} 不存在")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ {metadata_path} 格式错误: {e}")
        sys.exit(1)
    
    # 检查目标文件是否存在
    if not Path(target_plugins_json).exists():
        print(f"❌ {target_plugins_json} 不存在")
        print(f"   请确保目标仓库根目录包含 plugins.json")
        sys.exit(1)
    
    # 读取目标 JSON
    try:
        with open(target_plugins_json, 'r', encoding='utf-8') as f:
            plugins_data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ {target_plugins_json} 格式错误: {e}")
        sys.exit(1)
    
    if not isinstance(plugins_data, list):
        print("❌ plugins.json 格式错误：不是数组")
        sys.exit(1)
    
    # 查找并更新匹配的插件
    updated = False
    plugin_name = new_metadata.get('name')
    plugin_github = new_metadata.get('github')
    
    if not plugin_name:
        print("❌ 元数据中缺少 'name' 字段")
        sys.exit(1)
    
    if not plugin_github:
        print("❌ 元数据中缺少 'github' 字段")
        sys.exit(1)
    
    matched = False
    for plugin in plugins_data:
        if not isinstance(plugin, dict):
            continue
        
        # 优先通过 GitHub 地址匹配，备选通过名称匹配
        if (plugin_github and plugin.get('github') == plugin_github) or \
           (plugin_name and plugin.get('name') == plugin_name):
            
            matched = True
            # 更新字段
            changes = []
            for key in ['name', 'author', 'description', 'version', 'github']:
                if key in new_metadata and new_metadata[key]:
                    old_value = plugin.get(key)
                    new_value = new_metadata[key]
                    if old_value != new_value:
                        plugin[key] = new_value
                        changes.append(f"{key}: '{old_value}' -> '{new_value}'")
                        updated = True
            
            if changes:
                print(f"✅ 更新插件 '{plugin_name}':")
                for change in changes:
                    print(f"  {change}")
            else:
                print(f"ℹ️  插件 '{plugin_name}' 无变化")
            break
    
    if not matched:
        print(f"⚠️  未找到匹配的插件: {plugin_name}")
        print(f"   请先在目标仓库的 plugins.json 中添加该插件的条目")
        print(f"   匹配依据: github={plugin_github} 或 name={plugin_name}")
        sys.exit(0)  # 不报错，只是没有匹配到
    
    if updated:
        # 保存更新后的文件
        try:
            with open(target_plugins_json, 'w', encoding='utf-8') as f:
                json.dump(plugins_data, f, ensure_ascii=False, indent=2)
                f.write('\n')
            print(f"✅ 已更新 {target_plugins_json}")
        except Exception as e:
            print(f"❌ 保存文件失败: {e}")
            sys.exit(1)
    else:
        print("ℹ️  没有需要更新的内容")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python update_target_json.py <metadata.json>")
        sys.exit(1)
    
    update_plugins_json(sys.argv[1])
