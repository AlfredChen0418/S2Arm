# native-test-vpad_key_style：每键外观存储和按倍率取键（ios/vpad_layout.m，Foundation 层，Mac 上编）
NATIVE_TEST_OWN += vpad_key_style
.PHONY: native-test-vpad_key_style
native-test-vpad_key_style:
	@mkdir -p $(BUILD)/tests
	$(CC) $(CFLAGS) -fobjc-arc -framework Foundation -framework CoreGraphics -o $(BUILD)/tests/t_vpad_key_style runtime/tests/t_vpad_key_style.m ios/vpad_layout.m
	$(BUILD)/tests/t_vpad_key_style
TEST_TARGETS += native-test-vpad_key_style
