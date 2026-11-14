# native-test-nvn_fu_class：本单测的构建规则（runtime/build.mk 末尾按 runtime/tests/mk/*.mk 收进来；新单测只新增一个分片，不改 build.mk）
NATIVE_TEST_OWN += nvn_fu_class
ifeq ($(shell uname -s),Darwin)
# 首遇等待按预热清单分类（#1001：两级建管线之前先量种子 / 预热接住之后还剩多少首遇）
$(BUILD)/tests/t_nvn_fu_class: graphics/nvn/tests/t_nvn_fu_class.m graphics/nvn/gfx/nvn/nvn_mtl.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -o $@ graphics/nvn/tests/t_nvn_fu_class.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_fu_class
native-test-nvn_fu_class: $(BUILD)/tests/t_nvn_fu_class
	$(BUILD)/tests/t_nvn_fu_class
TEST_TARGETS += native-test-nvn_fu_class
endif
