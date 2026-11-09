"""把启动器里改的显示名和封面写进多款容器的目录清单。"""
import json
import shutil
from pathlib import Path


def apply_custom(app, games, path, games_dir):
    """启动器里改的显示名和封面写进容器；恢复默认后，已带自定义的条目换回原名原图。"""
    if not path:
        return
    custom = json.loads(Path(path).read_text(encoding="utf-8"))
    for e in games:
        item = custom.get(e["title_id"])
        if not item or not (item["custom"] or e.get("customized")):
            continue
        if item["name"]:
            e["name"] = item["name"]
        if item["cover"]:
            shutil.copyfile(item["cover"], app / games_dir / e["title_id"] / "icon.jpg")
            e["icon"] = f"{games_dir}/{e['title_id']}/icon.jpg"
        e.pop("customized", None)
        if item["custom"]:
            e["customized"] = True
