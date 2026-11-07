# native-test-vpad_stick_mode：摇杆模式和灵敏度（ios/vpad_layout.m，Foundation 层，Mac 上编）
NATIVE_TEST_OWN += vpad_stick_mode
.PHONY: native-test-vpad_stick_mode
native-test-vpad_stick_mode:
	@mkdir -p $(BUILD)/tests
	$(CC) $(CFLAGS) -fobjc-arc -framework Foundation -framework CoreGraphics -o $(BUILD)/tests/t_vpad_stick_mode runtime/tests/t_vpad_stick_mode.m ios/vpad_layout.m
	$(BUILD)/tests/t_vpad_stick_mode
TEST_TARGETS += native-test-vpad_stick_mode
