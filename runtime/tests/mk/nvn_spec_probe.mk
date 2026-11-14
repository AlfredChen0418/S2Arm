# native-test-nvn_spec_probe：本单测的构建规则（runtime/build.mk 末尾按 runtime/tests/mk/*.mk 收进来；新单测只新增一个分片，不改 build.mk）
NATIVE_TEST_OWN += nvn_spec_probe
ifeq ($(shell uname -s),Darwin)
# 两级建管线的耗时探针（#1001 第二步：基础管线 / 特化 / 完整建各自计时，缺省关）
$(BUILD)/tests/t_nvn_spec_probe: graphics/nvn/tests/t_nvn_spec_probe.m graphics/nvn/gfx/nvn/nvn_mtl.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -o $@ graphics/nvn/tests/t_nvn_spec_probe.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_spec_probe
native-test-nvn_spec_probe: $(BUILD)/tests/t_nvn_spec_probe
	$(BUILD)/tests/t_nvn_spec_probe
TEST_TARGETS += native-test-nvn_spec_probe
endif
