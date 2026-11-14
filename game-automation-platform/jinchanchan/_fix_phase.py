import sys

p = "C:/Users/admin/Desktop/jingchanchan_auto/agent/game_flow.py"
with open(p, "r", encoding="utf-8") as f:
    c = f.read()

old = '''        # Phase 2: 10s - buy heroes + exp
        print('')
        print('[Phase 2/3] 10s - Buy heroes and exp')
        phase2_end = cycle_start + 20

        print('  Buy heroes: click 5 shop slots once each')
        for i, pos in enumerate(self._shop_slots):
            if time.time() >= phase2_end:
                print('  Phase 2 timeout, stop buying heroes')
                break
            click_x = pos[0] - 30 if i == 0 else pos[0]
            print('    Click shop slot %d %s (x=%d)' % (i + 1, str(pos), click_x))
            click(click_x, pos[1], delay=0.3)

        if time.time() < phase2_end:
            print('  Buy exp: click button %s' % str(self._buy_exp_btn))
            click(self._buy_exp_btn[0], self._buy_exp_btn[1], delay=0.3)
        else:
            print('  Phase 2 timeout, skip buy exp')

        remaining = int(phase2_end - time.time())
        if remaining > 0:
            print('  Wait for phase 2 end, %ds remaining' % remaining)
            time.sleep(remaining)
        print('  Phase 2 done')

        # Phase 3: 10s - drag hero''';

new = '''        # Phase 2: 10s - buy heroes (each slot twice)
        print('')
        print('[Phase 2/3] 10s - Buy heroes (each slot x2)')
        phase2_end = cycle_start + 20

        print('  Buy heroes: click 5 shop slots twice each')
        for i, pos in enumerate(self._shop_slots):
            if time.time() >= phase2_end:
                print('  Phase 2 timeout, stop buying heroes')
                break
            click_x = pos[0] - 30 if i == 0 else pos[0]
            print('    Click shop slot %d %s (x=%d) - click 1' % (i + 1, str(pos), click_x))
            click(click_x, pos[1], delay=0.15)
            # 连续第二次点击
            print('    Click shop slot %d - click 2' % (i + 1))
            click(click_x, pos[1], delay=0.15)

        remaining = int(phase2_end - time.time())
        if remaining > 0:
            print('  Wait for phase 2 end, %ds remaining' % remaining)
            time.sleep(remaining)
        print('  Phase 2 done')

        # Phase 3: 10s - buy exp + drag hero
        print('')
        print('[Phase 3/3] 10s - Buy exp + drag hero')
        phase3_end = cycle_start + 30

        # 先识别购买经验按钮（两种模板）
        print('  Buy exp: looking for buy_experience or buy_experience_2...')
        exp_pos = find_image(ButtonImages.BUY_EXPERIENCE, confidence=0.45)
        if exp_pos:
            print('    Found buy_experience at %s, clicking' % str(exp_pos))
            click(exp_pos[0], exp_pos[1], delay=0.3)
        else:
            exp_pos = find_image(ButtonImages.BUY_EXPERIENCE_2, confidence=0.45)
            if exp_pos:
                print('    Found buy_experience_2 at %s, clicking' % str(exp_pos))
                click(exp_pos[0], exp_pos[1], delay=0.3)
            else:
                print('    No buy exp button found, skip')

        # 然后拖动英雄''';

c = c.replace(old, new)

with open(p, "w", encoding="utf-8") as f:
    f.write(c)
compile(c, "gf.py", "exec")
print("OK")