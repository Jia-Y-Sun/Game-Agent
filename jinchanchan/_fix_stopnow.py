with open("C:/Users/admin/Desktop/jingchanchan_auto/agent/game_flow.py", "r", encoding="utf-8") as f:
    c = f.read()

# 找到两处 STOP_NOW（run方法和test_settlement都包含了），给它们加 confidence 参数
# 形如 img_click(ButtonImages.STOP_NOW, timeout=15)
c = c.replace(
    "img_click(ButtonImages.STOP_NOW, timeout=15)",
    'img_click(ButtonImages.STOP_NOW, timeout=15, confidence=0.35)'
)

with open("C:/Users/admin/Desktop/jingchanchan_auto/agent/game_flow.py", "w", encoding="utf-8") as f:
    f.write(c)
compile(c, "gf.py", "exec")
print("OK")