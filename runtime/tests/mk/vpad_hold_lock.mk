# ZL / ZR 按住锁定：状态机与按游戏保存的开关（Foundation 主机测试）。
NATIVE_TEST_OWN += vpad_hold_lock
.PHONY: native-test-vpad_hold_lock
native-test-vpad_hold_lock:
	@mkdir -p $(BUILD)/tests
	$(CC) $(CFLAGS) -fobjc-arc -framework Foundation -framework CoreGraphics -o $(BUILD)/tests/t_vpad_hold_lock runtime/tests/t_vpad_hold_lock.m ios/vpad_layout.m
	$(BUILD)/tests/t_vpad_hold_lock
TEST_TARGETS += native-test-vpad_hold_lock
