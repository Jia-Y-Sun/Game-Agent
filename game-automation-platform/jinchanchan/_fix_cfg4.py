with open("C:/Users/admin/Desktop/jingchanchan_auto/config.py", "r", encoding="utf-8") as f:
    c = f.read()

old = '    BUY_EXPERIENCE = os.path.join(BASE, "buy_experience.png")'
new = '    BUY_EXPERIENCE = os.path.join(BASE, "buy_experience.png")\n    BUY_EXPERIENCE_2 = os.path.join(BASE, "buy_experience_2.png")'
c = c.replace(old, new)

with open("C:/Users/admin/Desktop/jingchanchan_auto/config.py", "w", encoding="utf-8") as f:
    f.write(c)
compile(c, "cfg.py", "exec")
print("OK")