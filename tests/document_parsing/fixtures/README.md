# 公共/合成测试材料

test_real_formats.py 在内存生成 DOCX 标题/表格、合并表头 XLSX、PPTX，以及中文扫描图。
不包含企业资料、考核题、客户截图或企业词典。
中文扫描测试需设置 KNOWSEAM_TEST_CHINESE_FONT 为有许可的本机 CJK 字体路径；字体不入仓。
生成的图片同时输出 PNG/JPEG/TIFF 和扫描 PDF，通过真实 Unix parser 客户端读取。
test_generic_e2e.py 使用临时源目录和唯一 company/source/collection，并仅接受 knowseam_test。
真实解析需 KNOWSEAM_TEST_PARSER_SOCKET；没配置会明确 skip，不能当作 OCR 已通过。
