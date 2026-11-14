# native-test-nvn_dsnap_slots：本单测的构建规则（runtime/build.mk 末尾按 runtime/tests/mk/*.mk 收进来；新单测只新增一个分片，不改 build.mk）
NATIVE_TEST_OWN += nvn_dsnap_slots
ifeq ($(shell uname -s),Darwin)
# 同遍深度采样的快照存储按形状复用（11-13 0400486024DEC000 Mac：两张深度交替采样，快照存储每帧来回重建）
$(BUILD)/tests/t_nvn_dsnap_slots: graphics/nvn/tests/t_nvn_dsnap_slots.m graphics/nvn/gfx/nvn/nvn_mtl.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -o $@ graphics/nvn/tests/t_nvn_dsnap_slots.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_dsnap_slots
native-test-nvn_dsnap_slots: $(BUILD)/tests/t_nvn_dsnap_slots
	$(BUILD)/tests/t_nvn_dsnap_slots
TEST_TARGETS += native-test-nvn_dsnap_slots
endif
