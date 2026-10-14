# 许可、模型来源与第三方声明

本程序自己的代码采用 **MIT License**，版权声明及完整条款见随包 `LICENSE`。
第三方库、模型、原生运行组件分别保留原来的许可；本程序的 MIT 许可不替代这些条款，也不授予题目、截图、题库内容或第三方商标的使用权。

完整原文保存在 `许可证/third_party/`，组件版本、许可文件路径及 SHA-256 清单见该目录的 `installed_distribution_notices.json`。这些原文按对应上游发行版复制保留，未翻译或改写。构建时还会生成实际修改过的原生库记录。

## PP-OCRv5 模型

本程序使用 PaddleOCR 的 **PP-OCRv5 mobile 检测模型**与 **PP-OCRv5 server 识别模型**，由 RapidAI/RapidOCR 转换为 ONNX 并通过其 v3.10.0 模型目录获取。上游权重版权归 Baidu 及适用的 PaddleOCR 权利人所有，转换、封装及 RapidOCR 代码的权利分别归对应作者所有。转换不转移原权重的所有权。

| 随包模型 | RapidOCR 获取来源 | SHA-256 |
| --- | --- | --- |
| `ch_PP-OCRv5_det_mobile.onnx` | [ModelScope / RapidAI/RapidOCR v3.10.0](https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx) | `4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae` |
| `ch_PP-OCRv5_rec_server.onnx` | [ModelScope / RapidAI/RapidOCR v3.10.0](https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx) | `e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a` |

许可为 **Apache License 2.0**。2026 年 10 月 14 日核对的 PaddlePaddle 官方对应模型卡明确声明 `apache-2.0`：

- [PP-OCRv5_mobile_det，固定修订](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_det/blob/0d63e78e2b680928f6b1747d76a08db6e645efb7/README.md)
- [PP-OCRv5_server_rec，固定修订](https://huggingface.co/PaddlePaddle/PP-OCRv5_server_rec/blob/b26c3587fda8da3c8ec0ce357214b4d661ff1558/README.md)
- [官方检测 ONNX 模型卡，固定修订](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_det_onnx/blob/e6f4fa85f00e168c862bc462aebca69eef9b3d3d/README.md)
- [官方识别 ONNX 模型卡，固定修订](https://huggingface.co/PaddlePaddle/PP-OCRv5_server_rec_onnx/blob/b70df217f4fd99d14f970bad092cebe7d74cc4d1/README.md)

原始模型项目与转换目录：[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)、[PaddleOCR LICENSE](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE)、[RapidOCR v3.10.0 模型目录](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/python/rapidocr/default_models.yaml)。许可核对事实另存于 `许可证/third_party/PP_OCRv5_LICENSE_EVIDENCE.json`。

随包 ONNX 文件的身份以 `models/manifest.json` 中的获取地址、大小及 SHA-256 为准；RapidOCR 转换结果不保证与 PaddlePaddle 单独托管的 ONNX 文件逐字节相同。完整 Apache-2.0 条款保存在 `models/LICENSE-APACHE-2.0.txt` 和 `许可证/third_party/runtime/OCR-Models-Apache-2.0.txt`。原上游模型声明文件照原文保留；其中 PP-OCRv6 的模型表不是本程序 PP-OCRv5 的身份清单。发布者未修改这两个已经获取的 ONNX 文件。

## 主要第三方组件

| 组件及已核对版本 | 适用许可与相关原生依赖 |
| --- | --- |
| RapidOCR 3.10.0 | Apache-2.0；模型权重的来源、所有权另见上文 |
| ONNX Runtime 1.31.0 | MIT；完整嵌入组件声明见原 `ThirdPartyNotices.txt` |
| OpenCV Python Headless 4.11.0.86 | Python 封装许可及 OpenCV Apache-2.0；保留原 `LICENSE-3RD-PARTY.txt` 中适用的嵌入库声明 |
| NumPy 1.26.4 / OpenBLAS | BSD 系列许可；原 notice 还列有 GCC Runtime Library Exception、libgfortran 与 LGPL 的 libquadmath |
| Shapely 2.2.0 / GEOS 3.14.1 | Shapely BSD-3-Clause，GEOS LGPL-2.1；原 Windows wheel 的许可说明一并保留 |
| lxml 6.1.3 | BSD-3-Clause；libxml2 2.11.9、libxslt 1.1.45、zlib 1.3.2 的原许可及静态 libiconv 1.17.1 的 LGPL-2.1 条款分别适用 |
| Pillow 12.3.0 | MIT-CMU，原文件还包含 JPEG、FreeType、WebP 等嵌入组件声明 |
| python-docx 1.2.0、mss 10.2.0、PyYAML 6.0.3、OmegaConf 2.4.0、colorlog 6.12.0、colorama 0.4.6 | 各自的 MIT 原文；OmegaConf 同时保留其 ANTLR attribution |
| PyAutoGUI 0.9.54、PyGetWindow 0.0.9、PyMsgBox 2.0.1、PyRect 0.2.0、pyperclip 1.11.0 | 各自的 BSD 原文 |
| PyScreeze 1.0.1、pytweening 1.2.0、pyclipper 1.4.0 | MIT；pyclipper 中 Clipper 6.4.2 另采用 Boost Software License 1.0 |
| certifi 2026.7.22、tqdm 4.70.1 | MPL-2.0；tqdm 同时保留其 MIT 声明；对应源文件另行提供 |
| requests 2.34.2、urllib3 2.8.0、idna 3.20、charset-normalizer 3.5.2 | 分别为 Apache-2.0（含 NOTICE）、MIT、BSD-3-Clause、MIT |
| packaging 26.3、typing_extensions 4.16.0、setuptools 84.0.0 | 分别为 Apache-2.0 或 BSD-2-Clause、PSF-2.0、MIT；setuptools 随带的 vendor 许可同时保留 |
| CPython 3.12.14、Tcl/Tk 8.6.12、OpenSSL 3.5.8、libffi | PSF、Tcl/Tk 原条款、Apache-2.0、MIT。libffi 的编译版本未独立确认，随包保留其上游许可文本 |
| PyInstaller 6.22.3 | GPL 加 Bootloader Exception；嵌入的运行钩子采用 Apache-2.0。该例外允许按应用自身许可分发组合可执行文件 |

This software is based in part on the work of the Independent JPEG Group.

This software is based in part on the work of the FreeType Team (https://freetype.org/). The bundled FreeType portion is used under the FreeType License option.

Clipper 6.4.2：Copyright Angus Johnson 2010–2017，Boost Software License 1.0。原头文件声明及完整许可见 `许可证/third_party/runtime/`。

## 对应源码与原生库修改

GEOS、静态 libiconv/lxml、NumPy/OpenBLAS/GCC runtime 的相关源码和重建资料，以及 MPL 组件 certifi、tqdm 的源文件，作为独立附件提供：

- [v1.2.2-beta.2 Release](https://github.com/Mrchips233/soeasy-question-bank/releases/tag/v1.2.2-beta.2)
- [SoEasy_1.2.2-beta.2_ThirdPartySources.zip](https://github.com/Mrchips233/soeasy-question-bank/releases/download/v1.2.2-beta.2/SoEasy_1.2.2-beta.2_ThirdPartySources.zip)

源码包保留原版权、许可、构建项目及来源/哈希清单。`THIRD_PARTY_SOURCES.md` 说明获取、重建和替换兼容库的方法。该附件与程序包在同一 Release 提供，不要求使用者联系发布者才能取得源码，也不构成发布者代签的三年书面要约。

公开版由 MIT 许可的 `packaging_support.py` 把 GEOS 导入表中 hash 后缀的 Microsoft runtime 名称改为系统的 `msvcp140.dll`，并重新计算 PE checksum。GEOS 算法代码未重新编译或修改。修改脚本作为源码包 `build_materials/packaging_support.py` 提供；最终构建的具体文件、修改前后 SHA-256、旧/新导入名及说明记录在应用包的 **`_internal/licenses/GEOS_IMPORT_MODIFICATIONS.json`**，应以该实际构建记录和文件清单核对。源文件和当前编译环境中的原 wheel DLL 不会被原地改写。

允许使用者替换兼容的 LGPL 库，以及在调试对 LGPL 组件的修改所需范围内进行逆向工程。源码附件提供静态组合组件的完整源码和构建资料，以支持 LGPL-2.1 第 6(a) 条的重链接路径；本次审查没有重新编译所有上游库来证明逐字节重现，也没有把静态库重链接的实际成功作为已经完成的验证。

## Microsoft Visual C++ 运行库

Microsoft Visual C++ Runtime 属于 Microsoft 的专有组件，不能按本程序的 MIT 或第三方开源许可另行授权。**本公开包不包含 Microsoft Visual C++ runtime DLL 的副本**，包括来自 System32、CPython 目录或 Shapely wheel 的相关副本。缺少运行库时，请使用 Microsoft 官方页面或安装器，依据安装器显示的 Microsoft 条款安装：

- [最新受支持的 Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
- [Microsoft 官方 x64 安装器](https://aka.ms/vs/17/release/vc_redist.x64.exe)

## 发布范围和审查限制

本公开包排除了 MouseInfo、OpenCV 可选 FFmpeg 视频插件以及含私人题目的测试截图/测试模块。包内自检使用项目自制的公开示例。上游完整许可文件可能提及其发行版中的可选组件，实际随包内容应以本版构建文件清单、许可索引及修改记录为准。

这份声明记录已识别组件、来源、许可和修改，不构成第三方认可或不侵权的法律保证。采集的题目、平台条款、图片及其他内容权利需要依据实际来源另行判断。
