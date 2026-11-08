# 优课在线助手 / UOOC Helper

面向优课在线当前试卷页的 Windows 桌面工具。它连接专用 Edge 浏览器，读取当前页题目，调用用户配置的 AI 接口填写答案，并由用户核对后手动提交。

## 功能

- 手动登录及人机验证后连接浏览器，可暂时断开并重新连接。
- “按大题独立对话”模式：每道大题单独请求 AI，不传入其他大题的对话内容。
- “整页一次处理”模式：当前页小题放入一次请求，可在界面上切换。
- 识别选择题、阅读填空和音频题组。音频在本地用 Whisper 转写，同组小题共享转写内容。下载或转写失败时跳过该题组。
- 不自动提交试卷。

## Windows 便携版

1. 从 [Releases](https://github.com/kongfukeyong/uooc-helper/releases) 下载 ZIP，完整解压。
2. 双击 `start.cmd`。首次启动按引导填写自己的 AI API Key、接口地址和模型。
3. 在打开的 Edge 中登录优课在线并完成验证，回到程序点击“连接浏览器”。
4. 打开试卷，选好处理模式，点击“快速处理当前页”；检查答案后自行提交。

便携包包含 Python 运行环境、Edge 驱动、FFmpeg 和 Whisper base 模型。浏览器版本与打包驱动不一致时，Selenium 会尝试获取匹配的驱动。

## 从源码运行

在 Windows 上准备 Python 3.12、Microsoft Edge 和 FFmpeg，然后安装依赖：

```powershell
python -m pip install -r requirements.txt
python app.py
```

源码版第一次处理音频时会下载 Whisper base 模型。程序支持网页中的 MP3、M4A、WAV 链接及 `audio`/`source` 地址；浏览器内部的 `blob:` 音频会跳过。

## 配置与隐私

仓库和 Release 仅包含空白的 `config.template.json`。用户在首次启动后输入的 API Key 保存在本机 `config.json`，该文件已被 `.gitignore` 排除。浏览器资料保存在本机 `data/edge-profile`，不会包含在 Release 压缩包中。

## 来源与许可证

本项目由 [YSJohnson/UnipusAI-Helper](https://github.com/YSJohnson/UnipusAI-Helper) 的修改版提取而来，保留原项目的 GNU AGPL-3.0 许可证。详情见 [LICENSE](LICENSE)。
