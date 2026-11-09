# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
NATIVE_TEST_OWN += nvn_rc_pending
ifeq ($(shell uname -s),Darwin)
NVN_RC_SOURCE ?= graphics/nvn/gfx/nvn/nvn_mtl.m
NVN_RC_BINARY ?= $(BUILD)/tests/t_nvn_rc_pending
$(NVN_RC_BINARY): graphics/nvn/tests/t_nvn_rc_pending.m $(NVN_RC_SOURCE) $(NVN_MTL_DEPS) $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -DNVN_RC_SOURCE='"$(abspath $(NVN_RC_SOURCE))"' \
	  -o $@ graphics/nvn/tests/t_nvn_rc_pending.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_rc_pending
native-test-nvn_rc_pending: $(NVN_RC_BINARY)
	NXGAL_BE=mtl3 $(NVN_RC_BINARY)
	NXGAL_BE=mtl4 $(NVN_RC_BINARY)
TEST_TARGETS += native-test-nvn_rc_pending
endif
