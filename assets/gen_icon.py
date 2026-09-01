"""用 Pillow 生成应用图标（icon.png / icon.ico），仅开发期需要运行。"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角底板（青蓝渐变近似：两层圆角矩形叠加）
d.rounded_rectangle([8, 8, 248, 248], radius=52, fill=(38, 132, 255, 255))
d.rounded_rectangle([8, 8, 248, 128], radius=52, fill=(64, 156, 255, 255))
d.rectangle([8, 100, 248, 160], fill=(0, 0, 0, 0))  # 让上半层只影响顶部（简单覆盖无效，忽略）

# 鼠标主体
d.rounded_rectangle([78, 52, 178, 172], radius=48, fill=(255, 255, 255, 255),
                    outline=(30, 90, 180, 255), width=4)
# 鼠标左右键分割线
d.line([128, 58, 128, 108], fill=(30, 90, 180, 255), width=4)
d.arc([98, 78, 158, 138], 180, 360, fill=(30, 90, 180, 255), width=4)
# 滚轮
d.rounded_rectangle([120, 60, 136, 96], radius=8, fill=(30, 90, 180, 255))

# 点击波纹
for r, alpha in ((70, 220), (88, 150)):
    d.ellipse([128 - r, 112 - r, 128 + r, 112 + r], outline=(255, 255, 255, alpha), width=6)

# 光标箭头
arrow = [(168, 150), (168, 216), (188, 196), (202, 226), (216, 218), (202, 190), (228, 188)]
d.polygon(arrow, fill=(255, 255, 255, 255), outline=(40, 40, 40, 255))

out_dir = os.path.dirname(os.path.abspath(__file__))
img.save(os.path.join(out_dir, "icon.png"))
ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(os.path.join(out_dir, "icon.ico"), sizes=ico_sizes)
print("icon.png / icon.ico generated")
