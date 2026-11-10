# native-test-nvn_rt_rgb9e5_view：本单测的构建规则（runtime/build.mk 末尾按 runtime/tests/mk/*.mk 收进来；新单测只新增一个分片，不改 build.mk）
NATIVE_TEST_OWN += nvn_rt_rgb9e5_view
ifeq ($(shell uname -s),Darwin)
# 04008E001F098000 RGB9E5 渲染目标经 R32UI 视图写：贴图建时带 PixelFormatView、视图挂得上、打包的字逐位读回；Metal 4 / 3 两路
$(BUILD)/tests/t_nvn_rt_rgb9e5_view: graphics/nvn/tests/t_nvn_rt_rgb9e5_view.m graphics/nvn/gfx/nvn/nvn_mtl.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -o $@ graphics/nvn/tests/t_nvn_rt_rgb9e5_view.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_rt_rgb9e5_view
native-test-nvn_rt_rgb9e5_view: $(BUILD)/tests/t_nvn_rt_rgb9e5_view
	NXGAL_BE=mtl4 $(BUILD)/tests/t_nvn_rt_rgb9e5_view
	NXGAL_BE=mtl3 $(BUILD)/tests/t_nvn_rt_rgb9e5_view
TEST_TARGETS += native-test-nvn_rt_rgb9e5_view
endif
