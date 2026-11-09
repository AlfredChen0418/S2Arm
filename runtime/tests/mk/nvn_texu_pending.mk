# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
NATIVE_TEST_OWN += nvn_texu_pending
ifeq ($(shell uname -s),Darwin)
NVN_TEXU_SOURCE ?= graphics/nvn/gfx/nvn/nvn_mtl.m
NVN_TEXU_BINARY ?= $(BUILD)/tests/t_nvn_texu_pending
$(NVN_TEXU_BINARY): graphics/nvn/tests/t_nvn_texu_pending.m $(NVN_TEXU_SOURCE) $(NVN_MTL_DEPS) $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -Wno-unused-function -Wno-unguarded-availability-new -Wno-unguarded-availability \
	  $(NATIVE_INC) -Igraphics/nvn/host -Igraphics/nvn/gfx/nvn -Igraphics/nvn/gfx/nvn/include -Igraphics/nvn/third_party/vulkan-headers/include \
	  -DNVN_TEXU_SOURCE='"$(abspath $(NVN_TEXU_SOURCE))"' \
	  -o $@ graphics/nvn/tests/t_nvn_texu_pending.m $(NVN_TEST_HOST_OBJS) $(NVN_TEST_LIB) $(NVN_TEST_WIDEN) \
	  -Wl,-rpath,@executable_path/nvn/widen $(NVN_TEST_WEAK) \
	  -framework Metal -framework Foundation -framework QuartzCore -framework CoreGraphics \
	  -framework ImageIO -framework UniformTypeIdentifiers -lc++
.PHONY: native-test-nvn_texu_pending
native-test-nvn_texu_pending: $(NVN_TEXU_BINARY)
	@result=0; for backend in mtl3 mtl4; do \
	  for scenario in default inline later cancel all collision; do \
	    if [ $$scenario = default ]; then NXGAL_BE=$$backend $(NVN_TEXU_BINARY) || result=1; \
	    else NXGAL_BE=$$backend $(NVN_TEXU_BINARY) --$$scenario || result=1; fi; \
	  done; done; exit $$result
TEST_TARGETS += native-test-nvn_texu_pending
endif
