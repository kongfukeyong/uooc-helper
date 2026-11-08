"""优课在线助手：仅处理当前试卷页，保留人工登录与提交。"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path

import whisper  # Load torch before Qt on Windows; the reverse order breaks c10.dll.
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)
from openai import OpenAI
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
CONFIG_TEMPLATE = ROOT / "config.template.json"
PROFILE = ROOT / "data" / "edge-profile"
PORT_FILE = ROOT / "data" / "edge-port.txt"
DEFAULTS = {
    "api_key": "", "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash", "max_tokens": 8192, "temperature": 0.3,
    "group_by_question": True,
}


def load_config():
    values = dict(DEFAULTS)
    source = CONFIG if CONFIG.exists() else CONFIG_TEMPLATE
    if source.exists():
        values.update(json.loads(source.read_text(encoding="utf-8")))
    return {key: values[key] for key in DEFAULTS}


def save_config(values):
    CONFIG.write_text(json.dumps({key: values[key] for key in DEFAULTS},
                                 ensure_ascii=False, indent=2), encoding="utf-8")


def edge_path():
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def debugger_ready(port):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            return "Edg" in json.load(response).get("Browser", "")
    except (OSError, ValueError):
        return False


class BrowserSession:
    def __init__(self):
        self.port = None
        self.driver = None
        self.process = None

    def open(self):
        PROFILE.mkdir(parents=True, exist_ok=True)
        try:
            old_port = int(PORT_FILE.read_text(encoding="ascii").strip())
            if debugger_ready(old_port):
                self.port = old_port
                return "已找到专用 Edge 浏览器。请完成登录验证后点击“连接浏览器”。"
        except (OSError, ValueError):
            pass
        edge = edge_path()
        if not edge:
            raise RuntimeError("未找到 Microsoft Edge")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.process = subprocess.Popen([
            str(edge), f"--user-data-dir={PROFILE}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.port}",
            "https://www.uooc.net.cn/",
        ], creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(40):
            if debugger_ready(self.port):
                PORT_FILE.write_text(str(self.port), encoding="ascii")
                return "专用 Edge 已打开。请手动登录并完成验证，然后点击“连接浏览器”。"
            time.sleep(0.5)
        raise RuntimeError("Edge 调试端口未启动；请关闭旧的专用 Edge 后重试")

    def connect(self):
        if self.driver:
            return
        if not debugger_ready(self.port):
            raise RuntimeError("专用 Edge 已关闭，请重新启动软件")
        options = webdriver.EdgeOptions()
        options.debugger_address = f"127.0.0.1:{self.port}"
        bundled_driver = ROOT / "drivers" / "msedgedriver.exe"
        if bundled_driver.is_file():
            try:
                self.driver = webdriver.Edge(service=Service(str(bundled_driver)), options=options)
                return
            except SessionNotCreatedException:
                pass  # Edge on another PC may have a newer version.
        self.driver = webdriver.Edge(options=options)

    def disconnect(self):
        driver, self.driver = self.driver, None
        if driver:
            driver.service.stop()

    def close(self):
        self.disconnect()


@dataclass
class Question:
    number: int
    kind: str
    text: str
    options: list = field(default_factory=list)
    element: object = None
    blank_label: str = ""


def element_text(element):
    return (element.text or element.get_attribute("textContent") or "").strip()


def audio_links(element):
    return element.find_elements(By.CSS_SELECTOR,
                                 'a[href*=".mp3"], a[href*=".m4a"], a[href*=".wav"], '
                                 'audio[src], audio source[src], source[src*=".mp3"]')


def transcribe_url(url, cache, driver=None):
    if url in cache:
        return cache[url]
    import tempfile
    path = None
    try:
        if not url or url.startswith("blob:"):
            raise ValueError("音频地址为空或是浏览器内部 blob 地址")
        headers = {}
        if driver is not None:
            # current_url may still point at the outer course page after entering
            # the exam iframe; location.href is the actual frame origin.
            headers["Referer"] = driver.execute_script("return location.href") or driver.current_url
            headers["User-Agent"] = driver.execute_script("return navigator.userAgent")
            host = urllib.parse.urlparse(url).hostname or ""
            cookies = [f"{cookie['name']}={cookie['value']}"
                       for cookie in driver.get_cookies()
                       if host == cookie.get("domain", "").lstrip(".")
                       or host.endswith("." + cookie.get("domain", "").lstrip("."))]
            if cookies:
                headers["Cookie"] = "; ".join(cookies)
        request = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=30) as response, tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as audio:
            path = Path(audio.name)
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ValueError("音频文件超过 100 MB")
                audio.write(chunk)
        model = cache.get("_model")
        if model is None:
            model = whisper.load_model("base", download_root=str(ROOT / "data" / "models"))
            cache["_model"] = model
        transcript = model.transcribe(str(path), language="en", fp16=False)["text"].strip()
        if not transcript:
            raise ValueError("音频未识别出文字")
        cache[url] = transcript
        return transcript
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def read_groups(driver, audio_cache, log, stopped):
    driver.switch_to.default_content()
    frames = driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="/exam/"]')
    if not frames:
        frames = driver.find_elements(By.CSS_SELECTOR, '[source-test] iframe')
    if frames:
        driver.switch_to.frame(frames[0])
    WebDriverWait(driver, 10).until(lambda d: d.find_elements(By.CSS_SELECTOR, ".testPaperShow .queContainer"))
    groups = []
    for container in driver.find_elements(By.CSS_SELECTOR, ".testPaperShow .queContainer"):
        if stopped.is_set():
            break
        blocks = container.find_elements(By.CSS_SELECTOR, ".ti")
        shared = []
        for block in blocks:
            if block.find_elements(By.CSS_SELECTOR, '.ti-alist input[type="radio"], .ti-alist input[type="checkbox"], .ti-alist label.ti-a textarea'):
                continue
            titles = block.find_elements(By.CSS_SELECTOR, ".ti-q-c")
            if titles:
                shared.append(element_text(titles[0]))
        failed_audio = False
        seen_audio = set()
        for link in audio_links(container):
            url = link.get_attribute("href") or link.get_attribute("src")
            if url in seen_audio:
                continue
            seen_audio.add(url)
            try:
                transcript = transcribe_url(url, audio_cache, driver)
                shared.append("【听力音频转写】" + transcript)
            except Exception as exc:
                log(f"音频转写失败：{str(exc)[:100]}")
                failed_audio = True
                break
        if failed_audio:
            log("跳过转写失败的题组，避免猜答听力题。")
            continue
        questions = []
        for block in blocks:
            if stopped.is_set():
                break
            choices = block.find_elements(By.CSS_SELECTOR, '.ti-alist input[type="radio"], .ti-alist input[type="checkbox"]')
            titles = block.find_elements(By.CSS_SELECTOR, ".ti-q-c")
            if not titles:
                continue
            title = element_text(titles[0])
            if choices:
                types = {choice.get_attribute("type") for choice in choices}
                names = {choice.get_attribute("name") for choice in choices}
                if len(types) != 1 or len(names) != 1:
                    log("跳过无法确定选项分组的小题。")
                    continue
                options = []
                for label in block.find_elements(By.CSS_SELECTOR, ".ti-alist label.ti-a"):
                    items = label.find_elements(By.CSS_SELECTOR, 'input[type="radio"], input[type="checkbox"]')
                    if not items:
                        continue
                    letter = (items[0].get_attribute("value") or "").upper()
                    if not re.fullmatch(r"[A-Z]", letter):
                        continue
                    content = label.find_elements(By.CSS_SELECTOR, ".ti-a-c")
                    options.append((letter, element_text(content[0] if content else label), items[0]))
                if options:
                    kind = "multiple" if "checkbox" in types else "single"
                    questions.append(Question(len(questions) + 1, kind, title, options=options))
                continue
            blank_number = 0
            for label in block.find_elements(By.CSS_SELECTOR, ".ti-alist label.ti-a"):
                fields = label.find_elements(By.CSS_SELECTOR, "textarea")
                if not fields:
                    continue
                blank_number += 1
                questions.append(Question(len(questions) + 1, "text",
                                          title if blank_number == 1 else "根据本题上方的同一篇阅读材料",
                                          element=fields[0], blank_label=element_text(label)))
        if questions:
            groups.append(("\n".join(x for x in shared if x), questions))
    return groups


def build_prompt(shared, questions, grouped=True):
    lines = (["同一组中的小题属于一道大题。先理解共享材料，再按小题编号分别作答。"]
             if grouped else ["以下是当前页的所有小题。按编号分别作答，材料紧邻相关小题。"])
    if shared:
        lines.extend(["【共享材料】", shared])
    for q in questions:
        if q.kind == "text":
            lines.append(f"{q.number}. 【填空】{q.text}\n{q.blank_label}。若题面给出带字母的备选句，只输出对应字母；否则只输出该空的内容。")
        else:
            lines.append(f"{q.number}. 【{'多选' if q.kind == 'multiple' else '单选'}】{q.text}")
            lines.extend(f"   {letter}. {text}" for letter, text, _ in q.options)
    lines.append("只按小题编号输出答案，每行一题，例如 1.A。多选题输出多个字母，填空题输出可直接填写的内容。不要解释。")
    return "\n".join(lines)


def make_batches(groups, group_by_question):
    if group_by_question:
        return groups
    flat = []
    for shared, questions in groups:
        for index, question in enumerate(questions):
            context = f"{shared}\n" if index == 0 and shared else ""
            flat.append(replace(question, number=len(flat) + 1,
                                text=context + question.text))
    return [("", flat)] if flat else []


def parse_answers(response, count):
    result = {}
    pattern = re.compile(r"(?<![\w$])(?:第|空)?\s*(\d+)\s*(?:题|空)?\s*[.、:：)\]]\s*")
    matches = list(pattern.finditer(response))
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(response)
        answer = response[match.end():end].strip()
        if 1 <= number <= count and answer:
            result[number] = answer.strip().strip("`\"'")
    if not result and count == 1:
        result[1] = response.strip()
    return result


def fill_answer(driver, question, answer):
    if question.kind == "text":
        field = question.element
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
        field.clear()
        field.send_keys(answer)
        driver.execute_script("""
            const field = arguments[0];
            field.dispatchEvent(new Event('input', {bubbles:true}));
            field.dispatchEvent(new Event('change', {bubbles:true}));
            field.dispatchEvent(new MouseEvent('mouseleave', {bubbles:false, view:window}));
        """, field)
        return (field.get_attribute("value") or "").strip() == answer.strip()
    valid = {letter for letter, _, _ in question.options}
    letters = re.findall(r"[A-Z]", answer.upper())
    if not letters or set(letters) - valid or (question.kind == "single" and len(letters) != 1):
        return False
    for letter, _, field in question.options:
        should_select = letter in letters
        if should_select != field.is_selected():
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
            try:
                ActionChains(driver).move_to_element(field).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", field)
    return all(field.is_selected() == (letter in letters) for letter, _, field in question.options)


def solve_current_page(driver, config, stopped, log):
    if not config["api_key"]:
        raise RuntimeError("请先在设置中填写 AI API Key")
    cache = {}
    try:
        groups = read_groups(driver, cache, log, stopped)
        total = sum(len(questions) for _, questions in groups)
        if not total:
            log("当前页没有找到可处理的优课在线题目。")
            return False
        grouped = bool(config.get("group_by_question", True))
        batches = make_batches(groups, grouped)
        if grouped:
            log(f"按大题独立处理：找到 {len(groups)} 道大题、{total} 道小题。")
        else:
            log(f"整页一次处理：找到 {total} 道小题。")
        client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
        completed = 0
        for index, (shared, questions) in enumerate(batches, 1):
            if stopped.is_set():
                break
            label = f"第 {index} 道大题" if grouped else "当前页"
            log(f"分析{label}（{len(questions)} 道小题）…")
            prompt = build_prompt(shared, questions, grouped=grouped)
            response = client.chat.completions.create(
                model=config["model"],
                # This request contains only the current batch. No history from a
                # previous parent question is sent, even when the client is reused.
                messages=[{"role": "system", "content": "你是英语练习题助手。严格按编号给出简洁答案。"},
                          {"role": "user", "content": prompt}],
                temperature=float(config["temperature"]),
                max_tokens=int(config["max_tokens"]),
            )
            content = (response.choices[0].message.content or "").strip()
            if not content:
                log(f"{label}的 AI 回答为空，请检查模型与 token 上限。")
                continue
            answers = parse_answers(content, len(questions))
            for question in questions:
                if stopped.is_set():
                    break
                answer = answers.get(question.number, "")
                if answer and fill_answer(driver, question, answer):
                    completed += 1
                else:
                    log(f"{label}第 {question.number} 小题未能填写。")
        log(f"已填写 {completed}/{total} 道小题。请核对后手动提交。")
        return completed == total
    finally:
        driver.switch_to.default_content()


class Signals(QObject):
    log = pyqtSignal(str)
    opened = pyqtSignal()
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    finished = pyqtSignal()


class SetupDialog(QDialog):
    """First-run guide; values are stored only after the user submits them."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("首次使用 · 配置 AI")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel("配置 AI 服务")
        title.setStyleSheet("font-size:20px; font-weight:700")
        layout.addWidget(title)
        description = QLabel("请输入你自己的 API Key。接口地址和模型已填入默认值，可按需要修改。")
        description.setWordWrap(True)
        layout.addWidget(description)
        fields = QGridLayout()
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText("在此粘贴 API Key")
        self.url_input = QLineEdit(config["base_url"])
        self.model_input = QLineEdit(config["model"])
        for row, (label, widget) in enumerate([
            ("API Key", self.key_input), ("接口地址", self.url_input), ("模型", self.model_input),
        ]):
            fields.addWidget(QLabel(label), row, 0)
            fields.addWidget(widget, row, 1)
        layout.addLayout(fields)
        self.hint = QLabel("填写后会保存在本机 config.json 中。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        later = QPushButton("稍后配置")
        later.clicked.connect(self.reject)
        save = QPushButton("保存并继续")
        save.setObjectName("primary")
        save.clicked.connect(self.accept_setup)
        actions.addStretch(1)
        actions.addWidget(later)
        actions.addWidget(save)
        layout.addLayout(actions)
        self.key_input.setFocus()

    def accept_setup(self):
        if not all((self.key_input.text().strip(), self.url_input.text().strip(),
                    self.model_input.text().strip())):
            self.hint.setText("请填写 API Key、接口地址和模型。")
            self.hint.setStyleSheet("color:#ffb57d")
            return
        self.accept()


class Window(QMainWindow):
    def __init__(self, auto_open=True):
        super().__init__()
        self.browser = BrowserSession()
        self.config = load_config()
        self.stopped = threading.Event()
        self.busy = False
        self._active_workers = 0
        self._close_requested = False
        self.signals = Signals()
        self.signals.log.connect(self.add_log)
        self.signals.opened.connect(self.on_opened)
        self.signals.connected.connect(self.on_connected)
        self.signals.disconnected.connect(self.on_disconnected)
        self.signals.finished.connect(self.on_finished)
        self.build_ui()
        if auto_open:
            QTimer.singleShot(0, self.begin)

    def begin(self):
        if not self.config["api_key"]:
            self.show_first_run()
        self.run_worker(self.open_browser)

    def show_first_run(self):
        dialog = SetupDialog(self.config, self)
        if dialog.exec_() != QDialog.Accepted:
            self.signals.log.emit("尚未配置 AI；可稍后在“AI 设置”中填写。")
            return False
        self.key_input.setText(dialog.key_input.text().strip())
        self.url_input.setText(dialog.url_input.text().strip())
        self.model_input.setText(dialog.model_input.text().strip())
        self.save_settings()
        return True

    def build_ui(self):
        self.setWindowTitle("优课在线助手")
        self.resize(790, 620)
        self.setMinimumSize(640, 510)
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#101923; color:#ecf3f7; font-family:'Microsoft YaHei UI'; font-size:14px; }
            QFrame#card { background:#1a2a38; border:1px solid #284154; border-radius:14px; }
            QLabel#heading { font-size:27px; font-weight:700; color:#fff; }
            QLabel#muted { color:#9eb2c1; }
            QLabel#status { color:#81e3be; font-weight:600; }
            QLineEdit { background:#0e202d; border:1px solid #355065; border-radius:7px; padding:8px; }
            QLineEdit:focus { border-color:#3ecdb2; }
            QPushButton { background:#2b4558; border:0; border-radius:8px; padding:10px 16px; font-weight:600; }
            QPushButton:hover { background:#36596f; }
            QPushButton:disabled { background:#203341; color:#78909f; }
            QPushButton#primary { background:#19a98f; color:#071a19; }
            QPushButton#primary:hover { background:#43d3b6; }
            QPushButton#mode:checked { background:#235e61; border:1px solid #35c9b1; }
            QTextEdit { background:#0e202d; border:1px solid #284154; border-radius:8px; padding:8px; }
        """)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(15)

        heading = QLabel("优课在线助手")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        subtitle = QLabel("连接浏览器 · 处理当前试卷 · 核对后提交")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(19, 17, 19, 17)
        card_layout.setSpacing(12)
        self.status = QLabel("正在打开专用 Edge 浏览器…")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        card_layout.addWidget(self.status)
        hint = QLabel("首次登录或出现人机验证时，请直接在 Edge 中完成。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        card_layout.addWidget(hint)
        self.mode_button = QPushButton()
        self.mode_button.setObjectName("mode")
        self.mode_button.setCheckable(True)
        self.mode_button.setChecked(bool(self.config["group_by_question"]))
        self.mode_button.setText("模式：按大题独立对话" if self.mode_button.isChecked()
                                 else "模式：整页一次处理")
        self.mode_button.setToolTip("点击切换；按大题模式下，每道大题的 AI 请求不带入其他大题内容。")
        self.mode_button.toggled.connect(self.set_group_mode)
        card_layout.addWidget(self.mode_button)
        buttons = QHBoxLayout()
        self.connection_button = QPushButton("连接浏览器")
        self.connection_button.setEnabled(False)
        self.connection_button.clicked.connect(self.toggle_connection)
        buttons.addWidget(self.connection_button)
        self.solve_button = QPushButton("快速处理当前页")
        self.solve_button.setObjectName("primary")
        self.solve_button.setEnabled(False)
        self.solve_button.clicked.connect(self.start_solving)
        buttons.addWidget(self.solve_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stopped.set)
        buttons.addWidget(self.stop_button)
        card_layout.addLayout(buttons)
        layout.addWidget(card)

        settings = QFrame()
        settings.setObjectName("card")
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(19, 15, 19, 15)
        settings_title = QLabel("AI 设置")
        settings_title.setStyleSheet("font-weight:700; font-size:17px")
        settings_layout.addWidget(settings_title)
        grid = QGridLayout()
        self.key_input = QLineEdit(self.config["api_key"])
        self.key_input.setEchoMode(QLineEdit.Password)
        self.url_input = QLineEdit(self.config["base_url"])
        self.model_input = QLineEdit(self.config["model"])
        for row, (label, widget) in enumerate([
            ("API Key", self.key_input), ("接口地址", self.url_input), ("模型", self.model_input),
        ]):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)
        settings_layout.addLayout(grid)
        save_button = QPushButton("保存设置")
        save_button.clicked.connect(self.save_settings)
        settings_layout.addWidget(save_button, alignment=Qt.AlignRight)
        layout.addWidget(settings)

        log_title = QLabel("运行记录")
        log_title.setStyleSheet("font-weight:700; font-size:17px")
        layout.addWidget(log_title)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, 1)

    def add_log(self, message):
        self.log_box.append(time.strftime("%H:%M:%S") + "  " + message)

    def run_worker(self, function):
        self._active_workers += 1
        def run():
            try:
                function()
            except Exception as exc:
                self.signals.log.emit(f"操作失败：{str(exc)[:180]}")
            finally:
                self.signals.finished.emit()
        threading.Thread(target=run, daemon=True).start()

    def open_browser(self):
        self.signals.log.emit(self.browser.open())
        self.signals.opened.emit()

    def on_opened(self):
        self.status.setText("等待登录验证；完成后点击“连接浏览器”。")
        self.connection_button.setEnabled(True)

    def toggle_connection(self):
        if self.busy:
            return
        self.connection_button.setEnabled(False)
        if self.browser.driver:
            self.run_worker(self.disconnect_browser)
        else:
            self.run_worker(self.connect_browser)

    def connect_browser(self):
        self.browser.connect()
        self.signals.connected.emit()

    def disconnect_browser(self):
        self.browser.disconnect()
        self.signals.disconnected.emit()

    def on_connected(self):
        self.status.setText("已连接。打开试卷后点击“快速处理当前页”。")
        self.connection_button.setText("暂时断开")
        self.connection_button.setEnabled(True)
        self.solve_button.setEnabled(True)
        self.signals.log.emit("浏览器已连接。")

    def on_disconnected(self):
        self.status.setText("已暂时断开；可在 Edge 中手动操作，再重新连接。")
        self.connection_button.setText("重新连接")
        self.connection_button.setEnabled(True)
        self.solve_button.setEnabled(False)
        self.signals.log.emit("已暂时断开浏览器。")

    def set_group_mode(self, enabled):
        self.config["group_by_question"] = bool(enabled)
        save_config(self.config)
        self.mode_button.setText("模式：按大题独立对话" if enabled
                                 else "模式：整页一次处理")
        self.signals.log.emit("已切换到" + ("按大题独立对话" if enabled else "整页一次处理") + "模式。")

    def start_solving(self):
        if not self.browser.driver or self.busy:
            return
        if not self.key_input.text().strip() and not self.show_first_run():
            return
        self.save_settings()
        self.stopped.clear()
        self.busy = True
        self.mode_button.setEnabled(False)
        self.connection_button.setEnabled(False)
        self.solve_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setText("正在处理当前试卷…")
        self.run_worker(lambda: solve_current_page(self.browser.driver, self.config,
                                                   self.stopped, self.signals.log.emit))

    def on_finished(self):
        self._active_workers = max(0, self._active_workers - 1)
        if self.busy:
            self.busy = False
            self.mode_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.connection_button.setEnabled(True)
            self.solve_button.setEnabled(self.browser.driver is not None)
            self.status.setText("处理结束，请检查网页中的答案。")
        elif self.browser.driver is None:
            self.connection_button.setEnabled(bool(self.browser.port))
            self.connection_button.setText("重新连接" if self.browser.port else "连接浏览器")
            self.solve_button.setEnabled(False)
        else:
            self.connection_button.setEnabled(True)
            self.connection_button.setText("暂时断开")
            self.solve_button.setEnabled(True)
        if self._close_requested and self._active_workers == 0:
            self._close_requested = False
            self.close()

    def save_settings(self):
        self.config.update(api_key=self.key_input.text().strip(),
                           base_url=self.url_input.text().strip(),
                           model=self.model_input.text().strip())
        save_config(self.config)
        self.signals.log.emit("AI 设置已保存。")

    def closeEvent(self, event):
        self.stopped.set()
        if self._active_workers:
            self._close_requested = True
            self.status.setText("正在结束当前操作，完成后自动退出…")
            self.connection_button.setEnabled(False)
            self.solve_button.setEnabled(False)
            event.ignore()
            return
        self.browser.close()
        event.accept()


def main():
    os.environ.setdefault("NO_PROXY", "*")
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
