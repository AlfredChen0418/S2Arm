# native-test-cpatch_settings：社区补丁设置页数据层单测（真 cpatch.tsv 夹具在 runtime/tests/cpatch/）；runtime/build.mk 按 runtime/tests/mk/*.mk 收进来
NATIVE_TEST_OWN += cpatch_settings
.PHONY: native-test-cpatch_settings
native-test-cpatch_settings:
	@mkdir -p $(BUILD)/tests
	$(CC) $(CFLAGS) -fobjc-arc -framework Foundation -o $(BUILD)/tests/t_cpatch_settings runtime/tests/t_cpatch_settings.m ios/cpatch_model.m
	@rm -rf $(BUILD)/tests/zh-Hans.lproj && cp -R ios/l10n/zh-Hans.lproj $(BUILD)/tests/   # 中文名经 S2AL 取字符串表（#316）：命令行程序的主 bundle 是所在目录，只放 zh-Hans 一种，按中文期望核
	$(BUILD)/tests/t_cpatch_settings runtime/tests/cpatch
TEST_TARGETS += native-test-cpatch_settings
