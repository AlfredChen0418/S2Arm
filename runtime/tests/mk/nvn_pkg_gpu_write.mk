# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
NATIVE_TEST_OWN += nvn_pkg_gpu_write
ifeq ($(shell uname -s),Darwin)
$(BUILD)/tests/t_nvn_pkg_gpu_write: graphics/nvn/tests/t_nvn_pkg_gpu_write.m $(NVN_MTL_DEPS) $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -o $@ graphics/nvn/tests/t_nvn_pkg_gpu_write.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_pkg_gpu_write
native-test-nvn_pkg_gpu_write: $(BUILD)/tests/t_nvn_pkg_gpu_write
	@status=0; for backend in mtl3 mtl4; do \
	  NXGAL_BE=$$backend $(BUILD)/tests/t_nvn_pkg_gpu_write || status=1; \
	  NXGAL_BE=$$backend $(BUILD)/tests/t_nvn_pkg_gpu_write scaled || status=1; \
	done; exit $$status
TEST_TARGETS += native-test-nvn_pkg_gpu_write
endif
