# v1.2.2-beta.2

Windows 截图 OCR 练习题采集工具，可自动采集单选、多选、判断题，按题型分别保存 JSON 和 Word。首次使用和操作步骤见 [README](README.md)。

- 公开完整应用源码与构建脚本，项目自己的代码采用 MIT；第三方组件保留各自许可。
- 保留实时 OCR、两次查看答案、自动下一题恢复、题型分开保存与重采覆盖功能。
- 使用原创离线自检图片和原创图标；不附带真实第三方题库、私人配置或采集日志。
- 补齐依赖许可及第三方对应源码附件，排除未使用的 MouseInfo、FFmpeg 等组件。
- OCR 模型保持原版本；构建后验证实际本地 OCR、GUI、三种题型及 Word/JSON 导出。

下载 `SoEasy_1.2.2-beta.2_Windows_x64.zip` 并完整解压后运行 EXE，无需 Python 或 uv。首次使用须从 [微软官方页面](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) 安装 Visual C++ v14 x64 运行库；本包不捆绑微软运行库 DLL。

`SoEasy_1.2.2-beta.2_ThirdPartySources.zip` 供审阅、修改和重建相关依赖，运行软件无需下载。许可和重建说明见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md) 与 [THIRD_PARTY_SOURCES](THIRD_PARTY_SOURCES.md)。

本次验证在已安装微软运行库的 Windows x64 机器上完成。相关 LGPL 库尚未重新编译验证全部重建步骤；版权审查不能替代法律意见，也不授予第三方题库材料的再分发权。
