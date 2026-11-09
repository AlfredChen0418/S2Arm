# Real production draws and independent GPU colors; exact old-source input optional.
NATIVE_TEST_OWN += nvn_feedback_snapshot
ifeq ($(shell uname -s),Darwin)
NVN_FEEDBACK_SOURCE ?= graphics/nvn/gfx/nvn/nvn_mtl.m
NVN_FEEDBACK_BINARY ?= $(BUILD)/tests/t_nvn_feedback_snapshot
$(NVN_FEEDBACK_BINARY): graphics/nvn/tests/t_nvn_feedback_snapshot.m $(NVN_FEEDBACK_SOURCE) $(NVN_MTL_DEPS) $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -DNVN_FEEDBACK_SOURCE='"$(abspath $(NVN_FEEDBACK_SOURCE))"' \
	  -o $@ graphics/nvn/tests/t_nvn_feedback_snapshot.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_feedback_snapshot
native-test-nvn_feedback_snapshot: $(NVN_FEEDBACK_BINARY)
	NXGAL_BE=mtl4 $(NVN_FEEDBACK_BINARY)
	NXGAL_BE=mtl4 $(NVN_FEEDBACK_BINARY) --snap-all
	NXGAL_BE=mtl3 $(NVN_FEEDBACK_BINARY)
	NXGAL_BE=mtl3 $(NVN_FEEDBACK_BINARY) --snap-all
TEST_TARGETS += native-test-nvn_feedback_snapshot
endif
